from __future__ import annotations

import ipaddress
import json
import logging
import shlex
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import frappe
import requests
from frappe import _

from atlas.atlas.core.mesh_address import get_virtual_machine_mesh_address
from atlas.atlas.core.ssh import SSHRunner, wait_for_server
from atlas.atlas.doctype.ssh_task.ssh_task import SSHTask
from atlas.service.doctype.runway_cache_server.runway_cache_server import cache_lifecycle_lock

if TYPE_CHECKING:
	from atlas.service.doctype.runway_cache_server.runway_cache_server import RunwayCacheServer
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine


class RunwayCacheServerProvisioner:
	"""Install a pinned Runway package and publish its two distinct network paths."""

	def __init__(self, cache: RunwayCacheServer) -> None:
		self.cache = cache

	@property
	def virtual_machine(self) -> VirtualMachine:
		return frappe.get_doc("Virtual Machine", self.cache.virtual_machine)

	@property
	def mesh_address(self) -> str:
		return get_virtual_machine_mesh_address(self.virtual_machine)

	@property
	def steps(self) -> tuple[tuple[str, Callable[[], None]], ...]:
		return (
			("secure-shell", self.wait_for_ssh),
			("configuration", self.write_configuration),
			("installation", self.install_release),
			("operator-ca", self.read_operator_ca),
			("proxy-routes", self.push_proxy_routes),
			("readiness", self.wait_for_readiness),
			("dns", self.publish_dns),
		)

	def run(self) -> None:
		with cache_lifecycle_lock():
			self.cache = frappe.get_single("Runway Cache Server")
			if self.cache.status not in ("Pending", "Provisioning") or not self.cache.virtual_machine:
				return
			if self.virtual_machine.is_draft:
				return
			phase = "start"
			self.cache.status = "Provisioning"
			self.cache.failure_message = None
			self.cache.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep
			try:
				for phase, operation in self.steps:
					logging.getLogger("atlas.service.provisioning").info("Runway cache setup: %s", phase)
					operation()
				self.cache.status = "Active"
				self.cache.save(ignore_permissions=True)
			except Exception as error:
				self.cache.status = "Failed"
				self.cache.failure_message = f"{phase}: {error}"
				self.cache.save(ignore_permissions=True)
				frappe.db.commit()  # nosemgrep
				raise

	def wait_for_ssh(self) -> None:
		vm = self.virtual_machine
		wait_for_server(
			host=vm.ssh_host,
			users=("root",),
			timeout_seconds=600,
			poll_interval_seconds=5,
			proxy_command=vm.get_ssh_proxy_command(),
		)

	def configuration(self) -> dict[str, object]:
		"""Match Runway's cache-service JSON contract; credentials arrive through configure."""
		settings = frappe.get_single("Atlas Settings")
		return {
			"version": 1,
			"region_id": self.cache.central_region,
			"mesh_region_id": settings.region_id,
			"central_region_id": settings.region_id,
			"listen": f"[{self.mesh_address}]:{self.cache.guest_port}",
			"operator_listen": f"[{self.mesh_address}]:{self.cache.operator_port}",
			"central_issuer": self.cache.central_issuer,
			"central_jwks_url": self.cache.central_jwks_url,
			"central_url": self.cache.central_url.rstrip("/"),
			"central_ca_file": "/etc/runway/central-ca.pem" if self.cache.central_ca else "",
			"central_token_file": "/etc/runway/central-token",
			"audience": "runway-cache",
			"certificate_file": "/etc/runway/tls/certificate.pem",
			"private_key_file": "/etc/runway/tls/private-key.pem",
			"signing_key_file": "/etc/runway/signing-key.pem",
			"cache_data_ca_file": "/etc/runway/tls/ca.pem",
			"archive_bytes": 1 << 30,
		}

	def write_configuration(self) -> None:
		"""Write nonsecret inputs even when the release hash has not changed."""
		files = {
			"cache-service.json": json.dumps(self.configuration(), sort_keys=True),
			"central-ca.pem": self.cache.central_ca or "",
			"storage-ca.pem": self.cache.storage_ca or "",
		}
		command = "set -eu\numask 077\ninstall -d -m 0700 /etc/runway\n"
		for name, content in files.items():
			command += f"printf %s {shlex.quote(content)} > /etc/runway/{name}.tmp\n"
			command += f"mv /etc/runway/{name}.tmp /etc/runway/{name}\n"
		vm = self.virtual_machine
		result = SSHRunner(vm.ssh_host, proxy_command=vm.get_ssh_proxy_command()).run_command(command)
		if not result.is_success:
			frappe.throw(_("The cache VM rejected its configuration."))

	def install_release(self) -> None:
		task = SSHTask.create_for_script_file(
			target_type="Virtual Machine",
			target=self.cache.virtual_machine,
			script_path="install-service-package.sh",
			environment={
				"PACKAGE_NAME": "runway-cache",
				"PACKAGE_SETUP_SCRIPT": "setup.sh",
				"PACKAGE_DOWNLOAD_URL": self.cache.release_url,
				"PACKAGE_SHA256": self.cache.release_sha256,
				"RUNWAY_CONFIG": "/etc/runway/cache-service.json",
				"RUNWAY_STORAGE_CA_FILE": "/etc/runway/storage-ca.pem",
				"RUNWAY_DOMAIN": self.cache.domain,
			},
			timeout_seconds=1800,
			run_in_background=False,
		)
		self.cache.installation_task = task.name
		self.cache.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep
		if task.result is None or not task.result.is_success:
			frappe.throw(_("Runway installation failed. See SSH Task {0}.").format(task.name))
		# A skipped package still needs to reload changed configuration and prove readiness.
		vm = self.virtual_machine
		result = SSHRunner(vm.ssh_host, proxy_command=vm.get_ssh_proxy_command()).run_command(
			"set -eu\nsystemctl restart runway-cache-data.service runway-cache-service.service\n"
			"systemctl is-active --quiet runway-cache-data.service runway-cache-service.service"
		)
		if not result.is_success:
			frappe.throw(_("Runway cache services did not start."))

	def read_operator_ca(self) -> None:
		"""Read only the public CA; signing and TLS private keys stay on the VM."""
		from cryptography import x509

		vm = self.virtual_machine
		result = SSHRunner(vm.ssh_host, proxy_command=vm.get_ssh_proxy_command()).run_command(
			"cat /etc/runway/tls/ca.pem"
		)
		if not result.is_success:
			frappe.throw(_("Cannot read the cache operator CA."))
		x509.load_pem_x509_certificate(result.output.encode())
		self.cache.operator_ca = result.output
		self.cache.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep

	def push_proxy_routes(self) -> None:
		"""Apply the generated TLS route through the normal proxy configuration path."""
		from atlas.service.core.proxy.provisioning import ProxyServerProvisioner

		for name in frappe.get_all("Proxy Server", filters={"status": "Active"}, pluck="name"):
			ProxyServerProvisioner(frappe.get_doc("Proxy Server", name)).push_configuration()

	def wait_for_readiness(self) -> None:
		"""Prove the TLS operator path rejects an unauthenticated configure request."""
		deadline = time.monotonic() + 60
		while time.monotonic() < deadline:
			try:
				response = requests.post(
					f"https://{self.cache.domain}/configure", json={}, timeout=5, allow_redirects=False
				)
				if response.status_code == 401:
					return
			except requests.RequestException:
				pass
			time.sleep(2)
		frappe.throw(_("Runway cache operator listener did not become ready through the proxy."))

	def publish_dns(self) -> None:
		frappe.get_single("Atlas Settings").dns_provider_controller.upsert_service_record(
			self.cache.internal_domain, ipaddress.ip_address(self.mesh_address), 300
		)
