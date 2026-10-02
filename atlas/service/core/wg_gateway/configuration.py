from __future__ import annotations

import hashlib
import json
import shlex
from string import Template
from typing import TYPE_CHECKING

import frappe
from frappe import _

from atlas.service.core.control_cluster import ControlClusterCredentials

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server import (
		WireguardGatewayServer,
	)

CONFIG_PATH = "/etc/atlas/wireguard-gateway.toml"
DAEMON_UNIT = "atlas-wg-gateway-api.service"
PEER_HOSTS_MARKER = "# atlas-wireguard-peer"

CONFIG_TEMPLATE = Template(
	"""[gateway]
region_id = $region_id
node_id = "$node_id"
private_key = "$private_key"

$nodes

$auth

$cluster

[tls]
fullchain_pem = '''
$certificate
'''
private_key_pem = '''
$tls_private_key
'''
"""
)

WRITE_COMMAND_TEMPLATE = Template(
	"""set -eu
install -d -m 0750 /etc/atlas
install -m 0600 /dev/null $temporary_path
cat > $temporary_path <<'ATLAS_WIREGUARD_CONFIG_END'
$content
ATLAS_WIREGUARD_CONFIG_END
mv -f $temporary_path $config_path"""
)

# Peer names resolve to mesh addresses, so cluster traffic stays on the mesh.
APPLY_COMMAND_TEMPLATE = Template(
	"""set -eu
sed -i '/ $peer_hosts_marker$$/d' /etc/hosts
printf '%s' $peer_hosts >> /etc/hosts
systemctl enable $daemon_unit
systemctl restart $daemon_unit"""
)


class GatewayConfiguration:
	"""Render the configuration file of one WireGuard gateway node."""

	def __init__(
		self,
		gateway: WireguardGatewayServer,
		members: list[WireguardGatewayServer] | None = None,
	) -> None:
		self.gateway = gateway
		self.settings: AtlasSettings = frappe.get_single("Atlas Settings")
		self.members = members
		self.credentials = ControlClusterCredentials(self.settings, "wireguard_gateway")

	@property
	def content(self) -> str:
		"""Return the complete configuration file."""
		certificate = self.settings.get_password("wildcard_tls_certificate", raise_exception=False)
		tls_private_key = self.settings.get_password("wildcard_tls_private_key", raise_exception=False)
		if not certificate or not tls_private_key:
			frappe.throw(_("Atlas Settings holds no wildcard TLS certificate to send to a gateway."))

		return CONFIG_TEMPLATE.substitute(
			region_id=self.settings.region_id,
			node_id=self.gateway.name,
			private_key=self.gateway.get_password("private_key"),
			nodes="\n\n".join(self.get_node_section(node) for node in self.nodes),
			auth=self.credentials.get_auth_section(self.settings.wg_gateway_audience_id),
			cluster=self.credentials.get_cluster_section(self.gateway.name, self.peers),
			certificate=certificate.strip(),
			tls_private_key=tls_private_key.strip(),
		)

	@property
	def nodes(self) -> list[WireguardGatewayServer]:
		"""Return the members of the gateway cluster, including this node, in name order.

		A new node is a member only after provisioning configured it, so a slow install never blocks writes."""
		members = self.members
		if members is None:
			names = frappe.get_all("Wireguard Gateway Server", filters={"is_cluster_member": 1}, pluck="name")
			members = [frappe.get_doc("Wireguard Gateway Server", name) for name in names]
		if self.gateway.name not in {member.name for member in members}:
			members = [*members, self.gateway]
		return sorted(members, key=lambda member: member.name)

	@staticmethod
	def get_node_section(node: WireguardGatewayServer) -> str:
		return "\n".join(
			[
				"[[nodes]]",
				f"node_id = {json.dumps(node.name)}",
				f"gateway_id = {node.gateway_id}",
				f"endpoint = {json.dumps(node.get_domain())}",
				f"listen_port = {node.listen_port}",
				f"public_key = {json.dumps(node.gateway_public_key)}",
			]
		)

	@property
	def peers(self) -> list[dict[str, str]]:
		"""Return the cluster members with their HTTPS and mesh addresses."""
		return [
			{
				"node_id": node.name,
				"address": f"https://{node.get_domain()}",
				"mesh_address": node.wireguard_mesh_ipv6 or "",
			}
			for node in self.nodes
		]

	@property
	def digest(self) -> str:
		"""Return the digest of the configuration and its template."""
		values = (
			CONFIG_TEMPLATE.template,
			str(self.settings.region_id),
			self.settings.wg_gateway_audience_id,
			*self.credentials.digest_values,
			"\n".join(self.get_node_section(node) for node in self.nodes),
			json.dumps(self.peers, sort_keys=True),
			self.settings.get_password("wildcard_tls_certificate", raise_exception=False) or "",
			self.settings.get_password("wildcard_tls_private_key", raise_exception=False) or "",
		)
		return hashlib.sha256("\0".join(values).encode()).hexdigest()

	def get_write_command(self) -> str:
		"""Return the command that writes the configuration file."""
		return WRITE_COMMAND_TEMPLATE.substitute(
			config_path=CONFIG_PATH, temporary_path=f"{CONFIG_PATH}.tmp", content=self.content
		)

	def get_apply_command(self) -> str:
		"""Return the command that points peer names at the mesh and restarts the API."""
		peer_hosts = "".join(
			f"{peer['mesh_address']} {peer['address'].removeprefix('https://')} {PEER_HOSTS_MARKER}\n"
			for peer in self.peers
			if peer["mesh_address"]
		)
		return APPLY_COMMAND_TEMPLATE.substitute(
			peer_hosts_marker=PEER_HOSTS_MARKER, peer_hosts=shlex.quote(peer_hosts), daemon_unit=DAEMON_UNIT
		)


def push_configuration_to_active_gateways() -> None:
	"""Queue the current configuration for each active gateway."""
	for name in frappe.get_all("Wireguard Gateway Server", filters={"status": "Active"}, pluck="name"):
		frappe.get_doc("Wireguard Gateway Server", name).enqueue_configuration_push()


def reconcile_gateway_configurations() -> None:
	"""Push a changed configuration, such as a renewed certificate or a new member, to each active gateway."""
	for name in frappe.get_all("Wireguard Gateway Server", filters={"status": "Active"}, pluck="name"):
		gateway: WireguardGatewayServer = frappe.get_doc("Wireguard Gateway Server", name)
		if gateway.pushed_config_hash != GatewayConfiguration(gateway).digest:
			gateway.enqueue_configuration_push(enqueue_after_commit=False)
