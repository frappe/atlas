from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING
from uuid import UUID

import frappe
from frappe import _

from atlas.atlas.core.artifacts import get_download_url
from atlas.atlas.core.ssh import SSHRunner
from atlas.atlas.core.tls.metal import atlas_client_identity, ensure_server_certificate
from atlas.atlas.doctype.ssh_task.ssh_task import SSHTask

if TYPE_CHECKING:
	from atlas.atlas.core.ssh import SSHResult
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer

FAILURE_REASON_LINES = 3
FAILURE_REASON_LENGTH = 500
WIREGUARD_CONFIGURE_TIMEOUT_SECONDS = 300
METALD_INSTALL_TIMEOUT_SECONDS = 1_200
STORAGE_INSTALL_TIMEOUT_SECONDS = 600
METALD_SETUP_TIMEOUT_SECONDS = 3_600

MESH_PREFIX = 0xFDAB
# The prefix and the region take the first 32 bits of the address, so the low 96
# bits of the server UUID are the host part.
MESH_HOST_MASK = (1 << 96) - 1


class HostInstallation:
	"""Own the Atlas software installation on one server."""

	def __init__(self, server: "MetalServer") -> None:
		self.server = server

	def configure_wireguard(self) -> None:
		"""Configure WireGuard and store its public key."""
		if not self.server.private_network_interface:
			frappe.throw(_("Metal Server {0} needs a private network interface.").format(self.server.name))

		self.set_wireguard_ip_address()
		result = SSHTask.create_for_script_file(
			target_type=self.server.doctype,
			target=self.server.name,
			script_path="configure-wireguard.sh",
			environment={
				"WIREGUARD_ADDRESS": self.server.wireguard_ip_address,
				"WIREGUARD_LISTEN_PORT": self.server.port,
				"MESH_UPLINK_INTERFACE": self.server.private_network_interface,
			},
			timeout_seconds=WIREGUARD_CONFIGURE_TIMEOUT_SECONDS,
			run_in_background=False,
		).result
		if not result or not result.is_success:
			throw_script_failure(
				_("Could not configure WireGuard on server {0}.").format(self.server.name), result
			)

		public_key = result.output.partition("===PUBLIC_KEY_START===")[2]
		public_key = public_key.partition("===PUBLIC_KEY_END===")[0].strip()
		if not public_key:
			frappe.throw(_("Metal Server {0} reported no WireGuard public key.").format(self.server.name))
		self.server.db_set("wireguard_public_key", public_key)

	def install_metal(self) -> None:
		"""Install Metal and its host dependencies."""
		if not self.server.private_ipv4_address:
			frappe.throw(_("Metal Server {0} needs a private IPv4 address.").format(self.server.name))
		if not self.server.private_network_interface:
			frappe.throw(_("Metal Server {0} needs a private network interface.").format(self.server.name))

		settings = self.server.settings
		if not settings.metald_binary_x86_64_file or not settings.wg_mesh_binary_x86_64_file:
			frappe.throw(_("Atlas Settings needs the metald and Atlas WG Mesh binaries."))

		self.install_storage()
		self.install_tls_credentials()

		listen_address = settings.server_provider_controller.metald_listen_address(self.server)
		try:
			listen_address = str(ipaddress.IPv4Address(listen_address))
		except ipaddress.AddressValueError:
			frappe.throw(
				_("Metal Server {0} has an invalid address for the selected metald endpoint.").format(
					self.server.name
				),
			)
		result = SSHTask.create_for_script_file(
			target_type=self.server.doctype,
			target=self.server.name,
			script_path="install-metald.sh",
			environment={
				"METALD_DOWNLOAD_URL": get_download_url(settings.metald_binary_x86_64_file),
				"METALD_SHA256": settings.metald_binary_hash,
				"WG_MESH_DOWNLOAD_URL": get_download_url(settings.wg_mesh_binary_x86_64_file),
				"WG_MESH_SHA256": settings.wg_mesh_binary_hash,
				"LISTEN_ADDRESS": f"{listen_address}:9000",
				"ATLAS_COMMON_NAME": atlas_client_identity(settings),
				"COORDINATION_LISTEN_ADDRESS": f"[{self.server.wireguard_ip_address}]:9001",
				"MESH_UPLINK_INTERFACE": self.server.private_network_interface,
			},
			timeout_seconds=METALD_INSTALL_TIMEOUT_SECONDS,
			run_in_background=False,
		).result
		if not result or not result.is_success:
			throw_script_failure(
				_("Could not install metald on server {0}.").format(self.server.name), result
			)

	def install_storage(self) -> None:
		"""Create the storage pool and mount host state on it before any file is written there."""
		result = SSHTask.create_for_script_file(
			target_type=self.server.doctype,
			target=self.server.name,
			script_path="install-metal-storage.sh",
			environment={
				"STORAGE_POOL_DEVICE": self.server.settings.server_provider_controller.storage_pool_device(
					self.server
				),
			},
			timeout_seconds=STORAGE_INSTALL_TIMEOUT_SECONDS,
			run_in_background=False,
		).result
		if not result or not result.is_success:
			throw_script_failure(
				_("Could not prepare storage on server {0}.").format(self.server.name), result
			)

	def install_tls_credentials(self) -> None:
		"""Issue a current node certificate and install it. The script restarts a running Metal."""
		ca_certificate, certificate, private_key = ensure_server_certificate(self.server)
		result = SSHRunner(self.server.ssh_host).run_script(
			"install-metal-tls.sh",
			data={
				"METAL_TLS_CA_CERTIFICATE": ca_certificate,
				"METAL_TLS_CERTIFICATE": certificate,
				"METAL_TLS_PRIVATE_KEY": private_key,
			},
			timeout_seconds=METALD_INSTALL_TIMEOUT_SECONDS,
		)
		if not result.is_success:
			throw_script_failure(
				_("Could not install Metal TLS credentials on server {0}.").format(self.server.name),
				result,
			)

	def upgrade_metald(self) -> None:
		"""Replace the metald binary and restart its daemon."""
		settings = self.server.settings
		if not settings.metald_binary_x86_64_file:
			frappe.throw(_("Atlas Settings needs the metald binary."))

		result = SSHTask.create_for_script_file(
			target_type=self.server.doctype,
			target=self.server.name,
			script_path="upgrade-metald.sh",
			environment={
				"METALD_DOWNLOAD_URL": get_download_url(settings.metald_binary_x86_64_file),
				"METALD_SHA256": settings.metald_binary_hash,
			},
			timeout_seconds=METALD_INSTALL_TIMEOUT_SECONDS,
			run_in_background=False,
		).result
		if not result or not result.is_success:
			throw_script_failure(
				_("Could not upgrade metald on server {0}.").format(self.server.name), result
			)

	def set_wireguard_ip_address(self) -> None:
		"""Set the WireGuard IP address if it is empty."""
		if not self.server.wireguard_ip_address:
			self.server.db_set("wireguard_ip_address", self.wireguard_ip_address)

	@property
	def wireguard_ip_address(self) -> str:
		"""Return the host mesh address for this server."""
		region_id = self.server.settings.region_id
		if not 0 <= region_id <= 0xFFFF:
			frappe.throw(_("Atlas Settings region ID must fit in one IPv6 field."))

		host = UUID(self.server.name).int & MESH_HOST_MASK
		return str(ipaddress.IPv6Address((MESH_PREFIX << 112) | (region_id << 96) | host))


def throw_script_failure(message: str, result: "SSHResult | None") -> None:
	"""Report a failed host script with the reason the script printed."""
	if not result:
		frappe.throw(_("{0} The script did not run.").format(message))

	frappe.throw(
		_("{0} Exit code {1}. {2}").format(message, result.exit_code, get_failure_reason(result.output))
	)


def get_failure_reason(output: str) -> str:
	"""Return the last lines a host script printed. A script prints its reason last."""
	printed_lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
	if not printed_lines:
		return _("The script printed no output.")

	return " ".join(printed_lines[-FAILURE_REASON_LINES:])[:FAILURE_REASON_LENGTH]
