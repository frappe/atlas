from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

import frappe
from frappe import _

from atlas.atlas.core.ssh import SSHRunner
from atlas.metal_server.core.atlas_peer import (
	PEER_KEEPALIVE_SECONDS,
	WIREGUARD_KEY,
	AtlasPeer,
	generate_private_key,
	get_public_key,
	write_private_file,
)

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


class DevelopmentGateway:
	"""Run atlas-vm as the development gateway on one Metal host, and write the local link to it."""

	interface = "atlas-gateway"
	# atlas-vm owns these values.
	listen_port = 51821
	developer_address = "172.16.100.3"
	deploy_command = """set -eu
if [ -f /var/lib/atlas-vm/atlas-vm.toml ] && ! grep -qxF '[gateway]' /var/lib/atlas-vm/atlas-vm.toml; then
	echo "This host runs an Atlas VM." >&2
	exit 1
fi
install -d -m 0700 /var/lib/atlas-vm
umask 077
printf '%s' "$ATLAS_VM" > /usr/local/bin/atlas-vm
chmod 0755 /usr/local/bin/atlas-vm
printf '%s' "$ATLAS_VM_CONFIG" > /var/lib/atlas-vm/atlas-vm.toml
unset ATLAS_VM ATLAS_VM_CONFIG
if systemctl is-active --quiet atlas-pilot-vm.service; then
	atlas-vm setup
else
	atlas-vm create --config /var/lib/atlas-vm/atlas-vm.toml
fi"""

	def __init__(self, server: "MetalServer", settings: "AtlasSettings | None" = None) -> None:
		self.server = server
		self.settings = settings or cast("AtlasSettings", frappe.get_single("Atlas Settings"))
		self.atlas_peer = AtlasPeer(self.settings)

	@property
	def private_key_path(self) -> Path:
		"""Return the developer key for the outer link."""
		return self.atlas_peer.directory / f"{self.interface}.key"

	@property
	def config_path(self) -> Path:
		"""Return the wg-quick file of the outer link."""
		return self.atlas_peer.directory / f"{self.interface}.conf"

	def install(self, ssh_host: str | None = None) -> Path:
		"""Deploy the gateway over SSH and write the local wg-quick file."""
		if not self.private_key_path.is_file():
			write_private_file(self.private_key_path, generate_private_key() + "\n")

		configuration = (
			f"[vm]\nvcpu_count = 1\nmemory_mib = 1024\ndisk_gib = 8\n\n"
			f'[atlas]\nprivate_network_cidr = "{self.settings.private_network_cidr}"\n\n'
			f'[gateway]\ndeveloper_public_key = "{get_public_key(self.private_key_path.read_text())}"\n'
		)
		script = Path(frappe.get_app_path("atlas")).parent / "scripts/atlas-vm/atlas_vm.py"
		result = SSHRunner(ssh_host or self.server.ssh_host).run_command(
			self.deploy_command,
			data={"ATLAS_VM": script.read_text(), "ATLAS_VM_CONFIG": configuration},
			timeout_seconds=1800,
		)
		match = re.search(r"gateway public key: (\S+)", result.output)
		if not result.is_success or not match or not WIREGUARD_KEY.fullmatch(match.group(1)):
			frappe.throw(_("The gateway setup failed: {0}").format(result.output.strip()[-500:]))

		write_private_file(self.config_path, self.get_config(match.group(1)))
		return self.config_path

	def get_config(self, gateway_public_key: str) -> str:
		"""Return the outer link. It carries only the provider private network."""
		return (
			"\n".join(
				[
					"[Interface]",
					f"PrivateKey = {self.private_key_path.read_text().strip()}",
					f"Address = {self.developer_address}/32",
					"",
					"[Peer]",
					f"PublicKey = {gateway_public_key}",
					f"AllowedIPs = {self.settings.private_network_cidr}",
					f"Endpoint = {self.server.public_ipv4_address}:{self.listen_port}",
					f"PersistentKeepalive = {PEER_KEEPALIVE_SECONDS}",
				]
			)
			+ "\n"
		)
