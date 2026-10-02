from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import frappe
import requests
from frappe import _

from atlas.atlas.core.ssh import SSHRunner, wait_for_server
from atlas.atlas.doctype.ssh_task.ssh_task import SSHTask
from atlas.service.core.proxy.configuration import ProxyConfiguration
from atlas.service.core.service_package import HTTP_PROXY_PACKAGE

if TYPE_CHECKING:
	from atlas.service.doctype.proxy_server.proxy_server import ProxyServer
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine

INSTALL_TIMEOUT_SECONDS = 1_800
CONFIGURE_TIMEOUT_SECONDS = 300
SSH_TIMEOUT_SECONDS = 600
SSH_POLL_INTERVAL_SECONDS = 5
CONTROL_READY_TIMEOUT_SECONDS = 600
CONTROL_READY_POLL_INTERVAL_SECONDS = 1
CONTROL_REQUEST_TIMEOUT_SECONDS = 2
NODE_DNS_TTL_SECONDS = 3600
REGIONAL_DNS_TTL_SECONDS = 120
WILDCARD_DNS_TTL_SECONDS = 3600


class ProxyServerProvisioner:
	"""Run proxy setup and store its progress."""

	def __init__(self, proxy_server: "ProxyServer") -> None:
		self.proxy_server = proxy_server
		self.logger = logging.getLogger("atlas.service.provisioning")

	def run(self) -> None:
		"""Run each proxy setup step in order."""
		if not self.is_virtual_machine_ready:
			return

		phase = "start"
		self.proxy_server.status = "Provisioning"
		self.proxy_server.failure_message = None
		self.save_progress()
		try:
			for phase, operation in self.steps:
				self.run_step(phase, operation)

			self.proxy_server.status = "Active"
			self.proxy_server.is_provisioning_completed = 1
			self.save_progress()
			from atlas.service.core.proxy.configuration import push_configuration_to_active_proxies

			push_configuration_to_active_proxies()
		except Exception as error:
			self.proxy_server.status = "Failed"
			self.proxy_server.failure_message = f"{phase}: {error}"
			self.save_progress()
			frappe.db.commit()  # nosemgrep
			self.logger.exception(
				"Proxy provisioning failed",
				extra={"resource": self.proxy_server.name, "operation": "provision", "phase": phase},
			)
			frappe.log_error(title=f"Proxy provisioning failed for {self.proxy_server.name} during {phase}")
			raise

	@property
	def steps(self) -> tuple[tuple[str, Callable[[bool], None]], ...]:
		"""Return setup steps in execution order."""
		return (
			("virtual-machine", self.validate_virtual_machine),
			("dns", self.update_dns_record),
			("secure-shell", self.wait_for_ssh),
			("package", self.install_package),
			("configuration", self.push_configuration),
			("cluster-membership", self.push_cluster_membership),
			("control-readiness", self.wait_for_control_readiness),
			("global-dns", self.update_global_dns_record),
		)

	@property
	def is_virtual_machine_ready(self) -> bool:
		"""Report whether the proxy virtual machine left the draft state."""
		if not self.proxy_server.virtual_machine:
			return False

		virtual_machine = frappe.get_doc("Virtual Machine", self.proxy_server.virtual_machine)
		return not virtual_machine.is_draft

	def run_step(self, phase: str, operation: Callable[[bool], None]) -> None:
		"""Run one setup step and save its resulting Proxy Server fields."""
		self.logger.info(
			"Proxy provisioning step started",
			extra={"resource": self.proxy_server.name, "operation": "provision", "phase": phase},
		)
		operation(save=False)
		self.save_progress()

	def validate_virtual_machine(self, save: bool = True) -> None:
		"""Confirm that the proxy virtual machine is ready."""
		if not self.proxy_server.virtual_machine:
			frappe.throw(_("Proxy Server {0} has no virtual machine.").format(self.proxy_server.name))

		virtual_machine = frappe.get_doc("Virtual Machine", self.proxy_server.virtual_machine)
		if virtual_machine.is_draft:
			frappe.throw(
				_("Wait for Virtual Machine {0} creation reconciliation before you provision again.").format(
					virtual_machine.name
				)
			)

		self.save_progress(save)

	def update_dns_record(self, save: bool = True) -> None:
		"""Point the proxy domain at its public IPv4 address."""
		self.proxy_server = frappe.get_doc("Proxy Server", self.proxy_server.name, for_update=True)
		if self.proxy_server.status == "Archived":
			return

		address = self.proxy_server.public_ipv4
		if not address:
			frappe.throw(_("Proxy Server {0} has no public IPv4 address.").format(self.proxy_server.name))

		settings = frappe.get_single("Atlas Settings")
		settings.dns_provider_controller.upsert_a_record(
			self.proxy_server.get_domain(), address, ttl=NODE_DNS_TTL_SECONDS
		)
		self.save_progress(save)

	def update_global_dns_record(self, save: bool = True) -> None:
		"""Publish this proxy as a health-checked regional endpoint."""
		address = self.proxy_server.public_ipv4
		if not address:
			frappe.throw(_("Proxy Server {0} has no public IPv4 address.").format(self.proxy_server.name))

		settings = frappe.get_single("Atlas Settings")
		provider = settings.dns_provider_controller
		if not self.proxy_server.dns_health_check_id:
			self.proxy_server.dns_health_check_id = provider.create_https_health_check(
				address,
				self.proxy_server.get_domain(),
				"/healthz",
			)
		provider.upsert_multivalue_a_record(
			f"proxy.{settings.wildcard_domain}",
			self.proxy_server.name,
			address,
			self.proxy_server.dns_health_check_id,
			ttl=REGIONAL_DNS_TTL_SECONDS,
		)
		provider.upsert_cname_record(
			f"*.{settings.wildcard_domain}",
			f"proxy.{settings.wildcard_domain}",
			ttl=WILDCARD_DNS_TTL_SECONDS,
		)
		self.save_progress(save)

	def push_cluster_membership(self, save: bool = True) -> None:
		"""Send the new peer list to each active proxy before DNS publication."""
		for name in frappe.get_all("Proxy Server", filters={"status": "Active"}, pluck="name"):
			if name == self.proxy_server.name:
				continue
			proxy_server = frappe.get_doc("Proxy Server", name)
			ProxyServerProvisioner(proxy_server).push_configuration()
		self.save_progress(save)

	def wait_for_control_readiness(self, save: bool = True) -> None:
		"""Wait until the node has synchronized and OpenResty is ready."""
		deadline = time.monotonic() + CONTROL_READY_TIMEOUT_SECONDS
		url = f"https://{self.proxy_server.get_domain()}/readyz"
		while time.monotonic() < deadline:
			try:
				if requests.get(url, timeout=CONTROL_REQUEST_TIMEOUT_SECONDS).status_code == 204:
					self.save_progress(save)
					return
			except requests.RequestException:
				pass
			time.sleep(CONTROL_READY_POLL_INTERVAL_SECONDS)
		frappe.throw(_("Proxy Server {0} did not become ready.").format(self.proxy_server.name))

	def wait_for_ssh(self, save: bool = True) -> None:
		"""Wait until the guest answers as root through its host."""
		virtual_machine = self.virtual_machine
		wait_for_server(
			host=virtual_machine.ssh_host,
			users=("root",),
			timeout_seconds=SSH_TIMEOUT_SECONDS,
			poll_interval_seconds=SSH_POLL_INTERVAL_SECONDS,
			proxy_command=virtual_machine.get_ssh_proxy_command(),
		)
		self.save_progress(save)

	def push_configuration(self, save: bool = True) -> None:
		"""Write and apply the proxy configuration."""
		configuration = ProxyConfiguration(self.proxy_server)
		if self.proxy_server.pushed_config_hash == configuration.digest:
			self.save_progress(save)
			return

		virtual_machine = self.virtual_machine
		write = SSHRunner(
			virtual_machine.ssh_host, proxy_command=virtual_machine.get_ssh_proxy_command()
		).run_command(configuration.get_write_command(), timeout_seconds=CONFIGURE_TIMEOUT_SECONDS)
		if not write.is_success:
			frappe.throw(
				_("The proxy did not accept its configuration file: {0}").format(write.output.strip())
			)

		task = SSHTask.create_for_command(
			target_type="Virtual Machine",
			target=self.proxy_server.virtual_machine,
			command=configuration.get_apply_command(),
			timeout_seconds=CONFIGURE_TIMEOUT_SECONDS,
			run_in_background=False,
		)
		result = task.result
		if result is None or not result.is_success:
			frappe.throw(_("The proxy rejected its configuration. See SSH Task {0}.").format(task.name))

		self.proxy_server.pushed_config_hash = configuration.digest
		self.proxy_server.tls_expires_on = frappe.get_single("Atlas Settings").wildcard_tls_expires_on
		self.save_progress(save)

	def install_package(self, save: bool = True) -> None:
		"""Download the published package and run the proxy setup script."""
		environment = HTTP_PROXY_PACKAGE.get_install_environment()
		if self.proxy_server.installed_package_hash == environment["PACKAGE_SHA256"]:
			self.save_progress(save)
			return

		task = SSHTask.create_for_script_file(
			target_type="Virtual Machine",
			target=self.proxy_server.virtual_machine,
			script_path="install-service-package.sh",
			environment=environment,
			timeout_seconds=INSTALL_TIMEOUT_SECONDS,
			run_in_background=False,
		)
		result = task.result
		if result is None or not result.is_success:
			frappe.throw(_("The HTTP proxy install failed. See SSH Task {0}.").format(task.name))

		self.proxy_server.installed_package_hash = environment["PACKAGE_SHA256"]
		self.save_progress(save)

	@property
	def virtual_machine(self) -> VirtualMachine:
		"""Return the proxy guest."""
		return frappe.get_doc("Virtual Machine", self.proxy_server.virtual_machine)

	def save_progress(self, save: bool = True) -> None:
		"""Store the current proxy state."""
		if save:
			self.proxy_server.save(ignore_permissions=True)
