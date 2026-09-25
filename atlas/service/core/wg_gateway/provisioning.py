from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import frappe
import requests
from frappe import _

from atlas.atlas.core.ssh import wait_for_server
from atlas.atlas.doctype.ssh_task.ssh_task import SSHTask
from atlas.auth.issuer import issue_token
from atlas.service.core.service_package import WG_GATEWAY_PACKAGE
from atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server import (
	wireguard_gateway_lifecycle_lock,
)

if TYPE_CHECKING:
	from atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server import (
		WireGuardGatewayServer,
	)
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine

INSTALL_TIMEOUT_SECONDS = 1_800
SSH_TIMEOUT_SECONDS = 600
SSH_POLL_INTERVAL_SECONDS = 5
DAEMON_TIMEOUT_SECONDS = 300
DAEMON_POLL_INTERVAL_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 5


class WireGuardGatewayProvisioner:
	"""Make the gateway VM a network gateway, then install WireGuard and nftables."""

	def __init__(self, gateway: WireGuardGatewayServer) -> None:
		self.gateway = gateway
		self.logger = logging.getLogger("atlas.service.provisioning")

	def run(self) -> None:
		"""Run each gateway setup step in order."""
		with wireguard_gateway_lifecycle_lock(self.gateway.name):
			self.gateway = frappe.get_doc("WireGuard Gateway Server", self.gateway.name)
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

	@property
	def steps(self) -> tuple[tuple[str, Callable[[], None]], ...]:
		"""Return setup steps in execution order."""
		return (
			("secure-shell", self.wait_for_ssh),
			("installation", self.install_gateway),
			("gateway-api", self.publish_gateway_api),
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
		"""Return the gateway virtual machine."""
		return frappe.get_doc("Virtual Machine", self.gateway.virtual_machine)

	def wait_for_ssh(self) -> None:
		"""Wait for root SSH on the public IPv4 address."""
		wait_for_server(
			host=self.virtual_machine.ssh_host,
			users=("root",),
			timeout_seconds=SSH_TIMEOUT_SECONDS,
			poll_interval_seconds=SSH_POLL_INTERVAL_SECONDS,
		)

	def install_gateway(self) -> None:
		"""Install the gateway package with the regional signing authority."""
		settings = frappe.get_single("Atlas Settings")
		task = SSHTask.create_for_script_file(
			target_type="Virtual Machine",
			target=self.gateway.virtual_machine,
			script_path="install-service-package.sh",
			environment={
				**WG_GATEWAY_PACKAGE.get_install_environment(),
				"REGION_ID": settings.region_id,
				"GATEWAY_MESH": self.gateway.wireguard_mesh_ipv6,
				"LISTEN_PORT": self.gateway.listen_port,
				"JWKS_URL": settings.jwks_url,
				"GWGATEWAY_AUDIENCE": settings.wg_gateway_audience_id,
				"JWKS_ISSUERS": json.dumps(["central", settings.issuer]),
			},
			timeout_seconds=INSTALL_TIMEOUT_SECONDS,
			run_in_background=False,
		)
		self.gateway.installation_task = task.name
		self.save()

		result = task.result
		if result is None or not result.is_success:
			frappe.throw(_("WireGuard gateway installation failed. See SSH Task {0}.").format(task.name))

	@property
	def proxy_url(self) -> str:
		"""Return the regional Proxy control API URL."""
		return f"https://proxy.{frappe.get_single('Atlas Settings').wildcard_domain}"

	@property
	def daemon_url(self) -> str:
		"""Return the gateway daemon URL through the regional proxy."""
		return f"https://{self.gateway.name}.{frappe.get_single('Atlas Settings').wildcard_domain}"

	def daemon_headers(self) -> dict[str, str]:
		"""Return a short-lived gateway daemon token for the public key read."""
		settings = frappe.get_single("Atlas Settings")
		token = issue_token(
			settings,
			audience=settings.wg_gateway_audience_id,
			subject="atlas",
			scope="gateway:read",
		)
		return {"Authorization": f"Bearer {token}"}

	def proxy_headers(self) -> dict[str, str]:
		"""Return the current regional Proxy bearer credential."""
		password = frappe.get_single("Atlas Settings").get_password(
			"proxy_cluster_password", raise_exception=False
		)
		if not password:
			frappe.throw(_("Atlas Settings holds no Proxy cluster password."))
		return {"Authorization": f"Bearer {password}"}

	def update_proxy_routes(self) -> None:
		"""Map the gateway hostname to the virtual machine mesh address."""
		mesh_address = self.gateway.wireguard_mesh_ipv6
		if not mesh_address:
			frappe.throw(
				_("WireGuard Gateway Server {0} has no mesh IPv6 address.").format(self.gateway.name)
			)
		response = requests.patch(
			f"{self.proxy_url}/v1/sites/{self.gateway.name}",
			headers=self.proxy_headers(),
			json={"address": mesh_address},
			timeout=REQUEST_TIMEOUT_SECONDS,
		)
		if not response.ok:
			frappe.throw(
				_("The Proxy control API refused the {0} route with status {1}.").format(
					self.gateway.name, response.status_code
				)
			)

	def remove_proxy_routes(self) -> None:
		"""Remove the gateway hostname before virtual machine termination."""
		response = requests.delete(
			f"{self.proxy_url}/v1/sites/{self.gateway.name}",
			headers=self.proxy_headers(),
			timeout=REQUEST_TIMEOUT_SECONDS,
		)
		if not response.ok:
			frappe.throw(
				_("The Proxy control API refused {0} route removal with status {1}.").format(
					self.gateway.name, response.status_code
				)
			)

	def wait_for_daemon(self) -> None:
		"""Wait until the gateway daemon answers through the regional proxy."""
		deadline = time.monotonic() + DAEMON_TIMEOUT_SECONDS
		while time.monotonic() < deadline:
			try:
				response = requests.get(f"{self.daemon_url}/healthz", timeout=REQUEST_TIMEOUT_SECONDS)
				if response.ok and response.json().get("status") == "ok":
					return
			except (requests.RequestException, ValueError):
				pass
			time.sleep(DAEMON_POLL_INTERVAL_SECONDS)
		frappe.throw(_("The WireGuard gateway daemon did not answer through the Proxy."))

	def read_public_key(self) -> None:
		"""Read the gateway public key from its daemon."""
		public_key = ""
		try:
			response = requests.get(
				f"{self.daemon_url}/config", headers=self.daemon_headers(), timeout=REQUEST_TIMEOUT_SECONDS
			)
			if response.ok:
				public_key = str(response.json().get("public_key") or "").strip()
		except (requests.RequestException, ValueError):
			pass
		if not public_key:
			frappe.throw(_("The gateway reported no WireGuard public key."))
		self.gateway.gateway_public_key = public_key
		self.save()

	def publish_gateway_api(self) -> None:
		"""Register the proxy route, wait for the daemon, and read its public key."""
		self.update_proxy_routes()
		self.wait_for_daemon()
		self.read_public_key()

	def save(self) -> None:
		"""Store the current gateway state."""
		self.gateway.save(ignore_permissions=True)
