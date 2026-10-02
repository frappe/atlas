from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import frappe
import requests
from frappe import _

from atlas.atlas.core.ssh import SSHRunner, wait_for_server
from atlas.atlas.doctype.ssh_task.ssh_task import SSHTask
from atlas.service.core.service_package import WG_GATEWAY_PACKAGE
from atlas.service.core.wg_gateway.configuration import DAEMON_UNIT, GatewayConfiguration
from atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server import (
	wireguard_gateway_lifecycle_lock,
)

if TYPE_CHECKING:
	from atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server import (
		WireguardGatewayServer,
	)
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine

INSTALL_TIMEOUT_SECONDS = 1_800
CONFIGURE_TIMEOUT_SECONDS = 300
SSH_TIMEOUT_SECONDS = 600
SSH_POLL_INTERVAL_SECONDS = 5
READY_TIMEOUT_SECONDS = 600
READY_POLL_INTERVAL_SECONDS = 1
REQUEST_TIMEOUT_SECONDS = 2
NODE_DNS_TTL_SECONDS = 300
REGIONAL_DNS_TTL_SECONDS = 120
STOP_TIMEOUT_SECONDS = 60
# Metal states in which the VM runs no API.
STOPPED_STATES = frozenset({"created", "stopped", "destroyed"})


class WireGuardGatewayProvisioner:
	"""Set up one gateway node and add it to the regional gateway cluster."""

	def __init__(self, gateway: WireguardGatewayServer) -> None:
		self.gateway = gateway
		self.settings = frappe.get_single("Atlas Settings")
		self.logger = logging.getLogger("atlas.service.provisioning")

	def run(self) -> None:
		"""Run each setup step in order, then send the new membership to every node."""
		with wireguard_gateway_lifecycle_lock(self.gateway.name):
			self.gateway = frappe.get_doc("Wireguard Gateway Server", self.gateway.name)
			if self.gateway.status == "Failed" or not self.is_virtual_machine_ready:
				return

			phase = "start"
			self.gateway.status = "Provisioning"
			self.gateway.failure_message = None
			self.save()
			try:
				for phase, operation in self.steps:
					self.logger.info(
						"WireGuard gateway provisioning step started",
						extra={"resource": self.gateway.name, "operation": "provision", "phase": phase},
					)
					operation()

				self.gateway.status = "Active"
				self.save()
			except Exception as error:
				self.gateway.status = "Failed"
				self.gateway.failure_message = f"{phase}: {error}"
				self.save()
				frappe.db.commit()  # nosemgrep
				self.logger.exception(
					"WireGuard gateway provisioning failed",
					extra={"resource": self.gateway.name, "operation": "provision", "phase": phase},
				)
				frappe.log_error(title=f"WireGuard gateway provisioning failed during {phase}")
				raise

		from atlas.service.core.wg_gateway.configuration import push_configuration_to_active_gateways

		push_configuration_to_active_gateways()

	@property
	def steps(self) -> tuple[tuple[str, Callable[[], None]], ...]:
		"""Return setup steps in execution order. Membership reaches the other nodes before DNS lists this one."""
		return (
			("network-gateway", self.enable_network_gateway),
			("dns", self.update_dns_record),
			("secure-shell", self.wait_for_ssh),
			("package", self.install_package),
			("configuration", self.push_configuration),
			("cluster-membership", self.push_cluster_membership),
			("readiness", self.wait_for_readiness),
			("regional-dns", self.update_regional_dns_record),
		)

	@property
	def is_virtual_machine_ready(self) -> bool:
		"""Report whether the VM left the draft state and Metal holds its public IPv4 address."""
		if not self.gateway.virtual_machine:
			return False

		virtual_machine = self.virtual_machine
		if virtual_machine.is_draft:
			return False

		information = virtual_machine.get_metal_vm_info()
		return bool(information and information.desired.network.public_ipv4)

	@property
	def virtual_machine(self) -> VirtualMachine:
		return frappe.get_doc("Virtual Machine", self.gateway.virtual_machine)

	def enable_network_gateway(self) -> None:
		"""Let WG Mesh carry client source addresses without translation."""
		virtual_machine = self.virtual_machine
		if not virtual_machine.is_network_gateway:
			virtual_machine.set_network_gateway(True)

	def update_dns_record(self) -> None:
		"""Point the node name, the endpoint that devices connect to, at its public IPv4 address."""
		self.settings.dns_provider_controller.upsert_a_record(
			self.gateway.get_domain(), self.gateway.public_ipv4, ttl=NODE_DNS_TTL_SECONDS
		)

	def wait_for_ssh(self) -> None:
		virtual_machine = self.virtual_machine
		wait_for_server(
			host=virtual_machine.ssh_host,
			users=("root",),
			timeout_seconds=SSH_TIMEOUT_SECONDS,
			poll_interval_seconds=SSH_POLL_INTERVAL_SECONDS,
			proxy_command=virtual_machine.get_ssh_proxy_command(),
		)

	def install_package(self) -> None:
		"""Install the gateway package. The network values compile into its eBPF filter."""
		environment = WG_GATEWAY_PACKAGE.get_install_environment()
		if self.gateway.installed_package_hash == environment["PACKAGE_SHA256"]:
			return

		task = SSHTask.create_for_script_file(
			target_type="Virtual Machine",
			target=self.gateway.virtual_machine,
			script_path="install-service-package.sh",
			environment={
				**environment,
				"REGION_ID": self.settings.region_id,
				"GATEWAY_ID": self.gateway.gateway_id,
				"GATEWAY_MESH": self.gateway.wireguard_mesh_ipv6,
				"LISTEN_PORT": self.gateway.listen_port,
			},
			timeout_seconds=INSTALL_TIMEOUT_SECONDS,
			run_in_background=False,
		)
		result = task.result
		if result is None or not result.is_success:
			frappe.throw(_("The WireGuard gateway install failed. See SSH Task {0}.").format(task.name))

		self.gateway.installed_package_hash = environment["PACKAGE_SHA256"]
		self.save()

	def push_configuration(self) -> None:
		"""Write the node configuration and restart the gateway API."""
		configuration = GatewayConfiguration(self.gateway)
		if self.gateway.pushed_config_hash == configuration.digest:
			return

		virtual_machine = self.virtual_machine
		write = SSHRunner(
			virtual_machine.ssh_host, proxy_command=virtual_machine.get_ssh_proxy_command()
		).run_command(configuration.get_write_command(), timeout_seconds=CONFIGURE_TIMEOUT_SECONDS)
		if not write.is_success:
			frappe.throw(_("The gateway did not accept its configuration: {0}").format(write.output.strip()))

		task = SSHTask.create_for_command(
			target_type="Virtual Machine",
			target=self.gateway.virtual_machine,
			command=configuration.get_apply_command(),
			timeout_seconds=CONFIGURE_TIMEOUT_SECONDS,
			run_in_background=False,
		)
		result = task.result
		if result is None or not result.is_success:
			frappe.throw(_("The gateway rejected its configuration. See SSH Task {0}.").format(task.name))

		self.gateway.pushed_config_hash = configuration.digest
		self.save()

	def push_cluster_membership(self) -> None:
		"""Make this configured node a member, and send the new member list to every other active node."""
		self.gateway.is_cluster_member = 1
		self.save()
		frappe.db.commit()  # nosemgrep
		apply_membership_to_active_gateways(excluding=self.gateway.name)

	def stop_api(self) -> None:
		"""Stop the node's API, so it accepts no write with a member list that is about to change.

		SSH can fail while HTTPS still answers, so without a confirmed stop Metal stops the whole VM."""
		if not self.gateway.virtual_machine or not frappe.db.exists(
			"Virtual Machine", self.gateway.virtual_machine
		):
			return
		virtual_machine = self.virtual_machine
		try:
			result = SSHRunner(
				virtual_machine.ssh_host, proxy_command=virtual_machine.get_ssh_proxy_command()
			).run_command(f"systemctl stop {DAEMON_UNIT}", timeout_seconds=STOP_TIMEOUT_SECONDS)
			if result.is_success:
				return
		except OSError, subprocess.TimeoutExpired:
			pass
		self.stop_virtual_machine(virtual_machine)

	def stop_virtual_machine(self, virtual_machine: VirtualMachine) -> None:
		"""Ask Metal to stop the VM, and wait until Metal reports it stopped or gone."""
		from atlas.vm.core.vm_service import VirtualMachineService

		service = VirtualMachineService(virtual_machine)
		service.set_power_state("stopped")
		deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
		while time.monotonic() < deadline:
			information = service.get_information()
			if information is None or information.observed.state in STOPPED_STATES:
				return
			time.sleep(READY_POLL_INTERVAL_SECONDS)
		frappe.throw(
			_(
				"Metal did not confirm that Virtual Machine {0} stopped, so its gateway API may still answer."
			).format(virtual_machine.name)
		)

	def wait_for_readiness(self) -> None:
		"""Wait until the node has joined the cluster and can accept writes."""
		deadline = time.monotonic() + READY_TIMEOUT_SECONDS
		url = f"https://{self.gateway.get_domain()}/readyz"
		while time.monotonic() < deadline:
			try:
				if requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS).status_code == 204:
					return
			except requests.RequestException:
				pass
			time.sleep(READY_POLL_INTERVAL_SECONDS)
		frappe.throw(_("Wireguard Gateway Server {0} did not become ready.").format(self.gateway.name))

	def update_regional_dns_record(self) -> None:
		"""Add this node to the regional API name. The check needs a ready cluster member, not only a tunnel."""
		provider = self.settings.dns_provider_controller
		if not self.gateway.dns_health_check_id:
			self.gateway.dns_health_check_id = provider.create_https_health_check(
				self.gateway.public_ipv4, self.gateway.get_domain(), "/readyz"
			)
			self.save()
		provider.upsert_multivalue_a_record(
			self.gateway.get_regional_domain(),
			self.gateway.name,
			self.gateway.public_ipv4,
			self.gateway.dns_health_check_id,
			ttl=REGIONAL_DNS_TTL_SECONDS,
		)

	def save(self) -> None:
		self.gateway.save(ignore_permissions=True)


def apply_membership_to_active_gateways(excluding: str) -> None:
	"""Send the current member list to every other active node now, before the membership change matters.

	Clusters of up to three nodes need every member to acknowledge a write, so a stale list blocks writes."""
	for name in frappe.get_all("Wireguard Gateway Server", filters={"status": "Active"}, pluck="name"):
		if name != excluding:
			WireGuardGatewayProvisioner(frappe.get_doc("Wireguard Gateway Server", name)).push_configuration()
