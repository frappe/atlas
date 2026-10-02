from __future__ import annotations

import base64
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

import frappe
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from frappe import _

from atlas.atlas.core.mesh_address import (
	WIREGUARD_PREFIX,
	get_atlas_mesh_address,
	get_region_mesh_address_prefix,
	get_virtual_machine_mesh_address,
)

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

PEER_KEEPALIVE_SECONDS = 25
WIREGUARD_KEY = re.compile(r"[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=")


class AtlasPeer:
	"""Own the Atlas wg0 identity and the wg-quick file that a root timer applies."""

	interface = "atlas0"

	def __init__(self, settings: "AtlasSettings | None" = None) -> None:
		self.settings = settings or cast("AtlasSettings", frappe.get_single("Atlas Settings"))

	@property
	def directory(self) -> Path:
		"""Return the private directory that holds the wg-quick files."""
		return Path(frappe.get_site_path("private", "wireguard")).resolve()

	@property
	def config_path(self) -> Path:
		"""Return the wg-quick file that the timer applies."""
		return self.directory / f"{self.interface}.conf"

	def ensure_identity(self) -> None:
		"""Create the Atlas key once in Atlas Settings, so site backups keep it, and store the mesh address."""
		address = get_atlas_mesh_address(self.settings.region_id)
		if self.settings.wireguard_ip_address != address:
			self.settings.db_set("wireguard_ip_address", address)

		if self.settings.wireguard_public_key:
			return

		self.settings.wireguard_private_key = generate_private_key()
		self.settings.wireguard_public_key = get_public_key(self.settings.wireguard_private_key)
		self.settings.save(ignore_permissions=True, ignore_version=True)

	def write_config(self) -> bool:
		"""Write the wg-quick file with one peer for each host. Return whether it changed."""
		if not self.settings.wireguard_public_key:
			return False

		self.ensure_identity()
		config = self.get_config()
		if self.config_path.is_file() and self.config_path.read_text() == config:
			return False

		write_private_file(self.config_path, config)
		return True

	def get_config(self) -> str:
		"""Return the wg-quick file content."""
		region_id = self.settings.region_id
		sections = [
			"[Interface]",
			f"PrivateKey = {self.settings.get_password('wireguard_private_key')}",
			f"Address = {self.settings.wireguard_ip_address}/128",
			# wg syncconf adds no routes.
			f"PostUp = ip -6 route replace {WIREGUARD_PREFIX:x}:{region_id:x}::/32 dev %i",
			f"PostUp = ip -6 route replace {get_region_mesh_address_prefix(region_id)}::/64 dev %i",
		]
		if frappe.conf.atlas_wireguard_mtu:
			sections.append(f"MTU = {int(frappe.conf.atlas_wireguard_mtu)}")
		virtual_machines = self.get_tenant_zero_virtual_machines()
		for host in self.get_host_peers():
			allowed_addresses = [host.wireguard_ip_address, *virtual_machines.get(host.name, [])]
			sections += [
				"",
				"[Peer]",
				f"PublicKey = {host.wireguard_public_key}",
				f"AllowedIPs = {', '.join(f'{address}/128' for address in allowed_addresses)}",
				f"Endpoint = {host.endpoint_address}:{host.port}",
				f"PersistentKeepalive = {PEER_KEEPALIVE_SECONDS}",
			]
		return "\n".join(sections) + "\n"

	def get_tenant_zero_virtual_machines(self) -> dict[str, list[str]]:
		"""Return the mesh addresses of tenant-0 VMs by host."""
		addresses: dict[str, list[str]] = {}
		for virtual_machine in frappe.get_all(
			"Virtual Machine",
			# "is set" never matches the uuid column server. A VM without a server has no host peer.
			filters={"tenant_id": 0, "is_draft": 0},
			fields=["name", "tenant_id", "server"],
			order_by="name asc",
		):
			addresses.setdefault(virtual_machine.server, []).append(
				get_virtual_machine_mesh_address(virtual_machine, self.settings.region_id)
			)
		return addresses

	def get_host_peers(self) -> list[frappe._dict]:
		"""Return every configured host. Reject a host that reuses the Atlas key."""
		hosts = frappe.get_all(
			"Metal Server",
			filters={
				"status": ["!=", "Deleted"],
				"wireguard_public_key": ["is", "set"],
				"wireguard_ip_address": ["is", "set"],
				"private_ipv4_address": ["is", "set"],
			},
			fields=[
				"name",
				"wireguard_public_key",
				"wireguard_ip_address",
				"port",
				"private_ipv4_address as endpoint_address",
			],
			order_by="name asc",
		)
		for host in hosts:
			if self.settings.wireguard_public_key == host.wireguard_public_key:
				frappe.throw(_("Metal Server {0} uses the Atlas WireGuard public key.").format(host.name))
		return hosts


def write_atlas_peer_config() -> None:
	"""Refresh the Atlas wg-quick file."""
	AtlasPeer().write_config()


def generate_private_key() -> str:
	"""Return a new WireGuard private key."""
	private_bytes = X25519PrivateKey.generate().private_bytes(
		serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
	)
	return base64.b64encode(private_bytes).decode()


def get_public_key(private_key: str) -> str:
	"""Return the public key of one WireGuard private key."""
	key = X25519PrivateKey.from_private_bytes(base64.b64decode(private_key))
	public_bytes = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
	return base64.b64encode(public_bytes).decode()


def write_private_file(path: Path, content: str) -> None:
	"""Replace one file that only the site user can read."""
	path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
	file_descriptor, temporary_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
	with os.fdopen(file_descriptor, "w") as temporary:
		temporary.write(content)
	os.replace(temporary_path, path)
