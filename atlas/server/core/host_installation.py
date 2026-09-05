from __future__ import annotations

import hashlib
import ipaddress
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils.password import get_decrypted_password

from atlas.atlas.core.host_binaries import get_binary_download_url
from atlas.server.doctype.server_ssh_task.server_ssh_task import ServerSSHTask

if TYPE_CHECKING:
	from atlas.server.doctype.server.server import Server

WIREGUARD_OVERHEAD_BYTES = 60
WIREGUARD_CONFIGURE_TIMEOUT_SECONDS = 300
METALD_INSTALL_TIMEOUT_SECONDS = 1_200


class HostInstallation:
	"""Own the Atlas software installation on one server."""

	def __init__(self, server: "Server") -> None:
		self.server = server

	def configure_wireguard(self) -> None:
		"""Configure WireGuard and store its public key."""
		self.set_wireguard_ip_address()
		result = ServerSSHTask.create_for_script_file(
			server=self.server.name,
			script_path="configure-wireguard.sh",
			environment={
				"WIREGUARD_ADDRESS": self.server.wireguard_ip_address,
				"WIREGUARD_LISTEN_PORT": self.server.port,
				"WIREGUARD_MTU": self.server.settings.private_network_mtu - WIREGUARD_OVERHEAD_BYTES,
			},
			timeout_seconds=WIREGUARD_CONFIGURE_TIMEOUT_SECONDS,
			run_in_background=False,
		).result
		if not result or not result.is_success:
			frappe.throw(_("Could not configure WireGuard on server {0}.").format(self.server.name))

		public_key = result.output.partition("===PUBLIC_KEY_START===")[2]
		public_key = public_key.partition("===PUBLIC_KEY_END===")[0].strip()
		if not public_key:
			frappe.throw(_("Server {0} reported no WireGuard public key.").format(self.server.name))
		self.server.db_set("wireguard_public_key", public_key)

	def install_metal(self) -> None:
		"""Install Metal and its host dependencies."""
		if not self.server.private_ipv4_address:
			frappe.throw(_("Server {0} needs a private IPv4 address.").format(self.server.name))
		if not self.server.private_network_interface:
			frappe.throw(_("Server {0} needs a private network interface.").format(self.server.name))

		settings = self.server.settings
		if not settings.metald_binary_x86_64_file or not settings.wg_mesh_binary_x86_64_file:
			frappe.throw(_("Atlas Settings needs the metald and Atlas WG Mesh binaries."))

		token = get_decrypted_password("Server", self.server.name, "metald_api_token", raise_exception=False)
		if not token:
			token = frappe.generate_hash(length=128)
			self.server.metald_api_token = token
			self.server.save(ignore_permissions=True, ignore_version=True)

		result = ServerSSHTask.create_for_script_file(
			server=self.server.name,
			script_path="install-metald.sh",
			environment={
				"METALD_DOWNLOAD_URL": get_binary_download_url(settings.metald_binary_x86_64_file),
				"WG_MESH_DOWNLOAD_URL": get_binary_download_url(settings.wg_mesh_binary_x86_64_file),
				"METALD_AUTH_TOKEN_HASH": hashlib.sha256(token.encode()).hexdigest(),
				"LISTEN_ADDRESS": "0.0.0.0:9000",
				"STORAGE_POOL_DEVICE": settings.server_provider_controller.get_storage_pool_device(
					self.server
				),
				"MESH_UPLINK_INTERFACE": self.server.private_network_interface,
			},
			timeout_seconds=METALD_INSTALL_TIMEOUT_SECONDS,
			run_in_background=False,
		).result
		if not result or not result.is_success:
			frappe.throw(_("Could not install metald on server {0}.").format(self.server.name))

	def set_wireguard_ip_address(self) -> None:
		"""Set the WireGuard IP address if it is empty."""
		if not self.server.wireguard_ip_address:
			self.server.db_set("wireguard_ip_address", self.wireguard_ip_address)

	@property
	def wireguard_ip_address(self) -> str:
		"""Return the host mesh address for this server."""
		node_number = self.server.name.rsplit("-", 1)[-1]
		if not node_number.isdigit():
			frappe.throw(_("Server {0} has no node number in its name.").format(self.server.name))

		region_id = self.server.settings.region_id
		if not 0 <= region_id <= 0xFFFF:
			frappe.throw(_("Atlas Settings region ID must fit in one IPv6 field."))
		return str(ipaddress.IPv6Address((0xFDAB << 112) | (region_id << 96) | int(node_number)))
