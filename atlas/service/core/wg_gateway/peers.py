from __future__ import annotations

import re
from string import Template
from typing import TYPE_CHECKING, Any

import frappe
from frappe import _

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.atlas.doctype.ssh_task.ssh_task import SSHTask
from atlas.service.core.wg_gateway.address import get_client_fdac, tenant_fdaa_prefix

if TYPE_CHECKING:
	from atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server import (
		WireGuardGatewayServer,
	)

STATE_DIR = "/opt/atlas/wg-gateway"
PRIVATE_KEY_PATH = f"{STATE_DIR}/privatekey"
PEERS_CONF_PATH = f"{STATE_DIR}/peers.conf"
NFT_PATH = f"{STATE_DIR}/gateway.nft"
SYNC_TIMEOUT_SECONDS = 300
PUBLIC_KEY_PATTERN = re.compile(r"[A-Za-z0-9+/]{43}=")

# The private key never leaves the virtual machine, so the interface section
# is read from the installed key file and joined with the rendered peers.
SYNC_COMMAND_TEMPLATE = Template(
	"""set -eu
install -d -m 0750 $state_dir
IFS= read -r private_key < $privatekey_path
install -m 0600 /dev/null $peers_temporary
echo "[Interface]" > $peers_temporary
echo "PrivateKey = $$private_key" >> $peers_temporary
echo "ListenPort = $listen_port" >> $peers_temporary
cat >> $peers_temporary <<'ATLAS_WG_PEERS_END'
$peers_content
ATLAS_WG_PEERS_END
mv -f $peers_temporary $peers_path
install -m 0600 /dev/null $nft_temporary
cat > $nft_temporary <<'ATLAS_WG_NFT_END'
$nft_content
ATLAS_WG_NFT_END
mv -f $nft_temporary $nft_path
wg setconf wg0 $peers_path
nft -f $nft_path"""
)


def validate_public_key(public_key: object) -> str:
	"""Return the key or reject anything that is not a WireGuard public key."""
	if not isinstance(public_key, str) or not PUBLIC_KEY_PATTERN.fullmatch(public_key.strip()):
		frappe.throw(
			_("Public key must be a 44-character base64 WireGuard public key."),
			exc=AtlasUserError,
		)
		raise AssertionError
	return public_key.strip()


def active_gateway(name: object) -> WireGuardGatewayServer:
	"""Return an Active gateway with a virtual machine, or reject the request."""
	if not isinstance(name, str) or not frappe.db.exists("WireGuard Gateway Server", name):
		frappe.throw(_("WireGuard Gateway Server {0} does not exist.").format(name))
		raise AssertionError
	gateway: WireGuardGatewayServer = frappe.get_doc("WireGuard Gateway Server", name)
	if gateway.status != "Active" or not gateway.virtual_machine:
		frappe.throw(_("WireGuard Gateway Server {0} is not Active.").format(name))
	return gateway


def render_wireguard_conf(peers: list[dict[str, Any]]) -> str:
	"""Return the wg setconf content for the desired peers."""
	lines = ["# Managed by Atlas. Do not edit."]
	for peer in peers:
		lines += [
			"[Peer]",
			f"PublicKey = {peer['public_key']}",
			f"AllowedIPs = {peer['fdac_address']}/128",
			"",
		]
	return "\n".join(lines) + "\n"


def render_nft(peers: list[dict[str, Any]], region_id: int, gateway_mesh: str) -> str:
	"""Return the atlas_wg_gateway table with one tenant-wide rule per tenant."""
	by_tenant: dict[int, list[str]] = {}
	for peer in peers:
		by_tenant.setdefault(peer["tenant_id"], []).append(peer["fdac_address"])
	rules = ["\t\tct state established,related counter accept"]
	for tenant_id in sorted(by_tenant):
		sources = ", ".join(sorted(by_tenant[tenant_id]))
		rules.append(
			f"\t\tip6 saddr {{ {sources} }} ip6 daddr {tenant_fdaa_prefix(region_id, tenant_id)}"
			" ct state new counter accept"
		)
	accepts = "\n".join(rules)
	return f"""table ip6 atlas_wg_gateway {{}}
delete table ip6 atlas_wg_gateway

table ip6 atlas_wg_gateway {{
\tchain forward {{
\t\ttype filter hook forward priority filter; policy drop;
{accepts}
\t}}

\tchain postrouting {{
\t\ttype nat hook postrouting priority srcnat; policy accept;
\t\tip6 saddr fdac::/16 ip6 daddr fdaa::/16 counter snat to {gateway_mesh}
\t}}

\t# The gateway forwards out of the interface that received the packet. A redirect would send the host around it.
\tchain output {{
\t\ttype filter hook output priority filter; policy accept;
\t\ticmpv6 type nd-redirect drop
\t}}
}}
"""


def push(gateway_name: str) -> None:
	"""Replace the gateway WireGuard peers and firewall with the desired state."""
	gateway = active_gateway(gateway_name)
	settings = frappe.get_single("Atlas Settings")
	peers = list_peers(gateway.name)
	command = SYNC_COMMAND_TEMPLATE.substitute(
		state_dir=STATE_DIR,
		privatekey_path=PRIVATE_KEY_PATH,
		listen_port=gateway.listen_port,
		peers_temporary=f"{PEERS_CONF_PATH}.tmp",
		peers_content=render_wireguard_conf(peers),
		peers_path=PEERS_CONF_PATH,
		nft_temporary=f"{NFT_PATH}.tmp",
		nft_content=render_nft(peers, settings.region_id, gateway.wireguard_mesh_ipv6),
		nft_path=NFT_PATH,
	)
	task = SSHTask.create_for_command(
		target_type="Virtual Machine",
		target=gateway.virtual_machine,
		command=command,
		timeout_seconds=SYNC_TIMEOUT_SECONDS,
		run_in_background=False,
	)
	result = task.result
	if result is None or not result.is_success:
		frappe.throw(_("The gateway rejected the peer update. See SSH Task {0}.").format(task.name))


def add_peer(gateway: str, tenant_id: int, client_id: int, public_key: str) -> dict[str, str | int]:
	"""Add one Central-provided client to a gateway and sync it."""
	server = active_gateway(gateway)
	key = validate_public_key(public_key)
	settings = frappe.get_single("Atlas Settings")
	tenant_id = _coerced_int("Tenant ID", tenant_id)
	client_id = _coerced_int("Client ID", client_id)
	fdac = get_client_fdac(settings.region_id, tenant_id, client_id)
	existing_name = frappe.db.get_value(
		"WireGuard Gateway Peer",
		{"gateway": server.name, "tenant_id": tenant_id, "client_id": client_id},
		"name",
	)
	if existing_name:
		existing = frappe.get_doc("WireGuard Gateway Peer", existing_name)
		if existing.public_key != key:
			frappe.throw(
				_("Client {0} of tenant {1} already uses another public key.").format(client_id, tenant_id)
			)
		push(server.name)
		return {"name": existing.name, "fdac": existing.fdac_address}
	if frappe.db.exists("WireGuard Gateway Peer", {"gateway": server.name, "public_key": key}):
		frappe.throw(_("This public key is already a peer of gateway {0}.").format(server.name))
	peer = frappe.get_doc(
		{
			"doctype": "WireGuard Gateway Peer",
			"gateway": server.name,
			"tenant_id": tenant_id,
			"client_id": client_id,
			"public_key": key,
			"fdac_address": fdac,
		}
	)
	peer.flags.managed_by_wg_gateway_api = True
	peer.insert(ignore_permissions=True)
	push(server.name)
	return {"name": peer.name, "fdac": fdac}


def delete_peer(
	name: str | None = None, gateway: str | None = None, public_key: str | None = None
) -> dict[str, str | bool]:
	"""Delete one peer by name or by gateway and public key, then sync. Missing peers are gone."""
	peer_name = name
	if peer_name is None:
		if gateway is None or public_key is None:
			frappe.throw(_("Delete a peer by name or by gateway and public key."))
		peer_name = frappe.db.get_value(
			"WireGuard Gateway Peer", {"gateway": gateway, "public_key": public_key}, "name"
		)
	if peer_name is None or not frappe.db.exists("WireGuard Gateway Peer", peer_name):
		return {"name": name or "", "gone": True}
	peer = frappe.get_doc("WireGuard Gateway Peer", peer_name)
	server_name = peer.gateway
	peer.flags.managed_by_wg_gateway_api = True
	peer.delete(ignore_permissions=True)
	push(server_name)
	return {"name": peer_name, "gone": True}


def list_peers(gateway: str, tenant_id: int | None = None) -> list[dict[str, Any]]:
	"""Return the peers of one gateway, optionally for one tenant."""
	if not isinstance(gateway, str) or not frappe.db.exists("WireGuard Gateway Server", gateway):
		frappe.throw(_("WireGuard Gateway Server {0} does not exist.").format(gateway))
	filters: dict[str, Any] = {"gateway": gateway}
	if tenant_id is not None:
		filters["tenant_id"] = _coerced_int("Tenant ID", tenant_id)
	return [
		dict(row)
		for row in frappe.get_all(
			"WireGuard Gateway Peer",
			filters=filters,
			fields=["name", "tenant_id", "client_id", "public_key", "fdac_address"],
			order_by="name",
		)
	]


def _coerced_int(label: str, value: object) -> int:
	"""Return value as an int, accepting the strings HTTP delivers."""
	if isinstance(value, bool):
		frappe.throw(_("{0} must be an integer.").format(label))
		raise AssertionError
	try:
		return int(str(value))
	except (TypeError, ValueError):
		frappe.throw(_("{0} must be an integer.").format(label))
		raise AssertionError


def replace_peer_list(gateway: str, peers: list[dict[str, Any]]) -> dict[str, str | int]:
	"""Replace the complete peer list of one gateway with the desired list from Central."""
	server = active_gateway(gateway)
	if not isinstance(peers, list):
		frappe.throw(_("Peers must be a list of tenant, client, and public key objects."))
	wanted: dict[tuple[int, int], tuple[str, str]] = {}
	region_id = frappe.get_single("Atlas Settings").region_id
	for index, peer in enumerate(peers):
		if not isinstance(peer, dict):
			frappe.throw(_("Peer {0} must be an object.").format(index))
		key = validate_public_key(peer.get("public_key"))
		tenant_id = _coerced_int("Tenant ID", peer.get("tenant_id"))
		client_id = _coerced_int("Client ID", peer.get("client_id"))
		fdac = get_client_fdac(region_id, tenant_id, client_id)
		if (tenant_id, client_id) in wanted:
			frappe.throw(_("Client {0} of tenant {1} appears twice.").format(client_id, tenant_id))
		wanted[(tenant_id, client_id)] = (key, fdac)
	if len({key for key, _fdac in wanted.values()}) != len(wanted):
		frappe.throw(_("One public key appears for two clients."))
	current = {
		(peer.tenant_id, peer.client_id): peer
		for peer in frappe.get_all(
			"WireGuard Gateway Peer",
			filters={"gateway": server.name},
			fields=["name", "tenant_id", "client_id", "public_key"],
		)
	}
	added = removed = updated = 0
	for identity in current:
		if identity not in wanted:
			peer = frappe.get_doc("WireGuard Gateway Peer", current[identity].name)
			peer.flags.managed_by_wg_gateway_api = True
			peer.delete(ignore_permissions=True)
			removed += 1
	for (tenant_id, client_id), (key, fdac) in wanted.items():
		if (tenant_id, client_id) not in current:
			peer = frappe.get_doc(
				{
					"doctype": "WireGuard Gateway Peer",
					"gateway": server.name,
					"tenant_id": tenant_id,
					"client_id": client_id,
					"public_key": key,
					"fdac_address": fdac,
				}
			)
			peer.flags.managed_by_wg_gateway_api = True
			peer.insert(ignore_permissions=True)
			added += 1
		elif current[(tenant_id, client_id)].public_key != key:
			peer = frappe.get_doc("WireGuard Gateway Peer", current[(tenant_id, client_id)].name)
			peer.flags.managed_by_wg_gateway_api = True
			peer.public_key = key
			peer.save(ignore_permissions=True)
			updated += 1
	push(server.name)
	return {"gateway": server.name, "added": added, "removed": removed, "updated": updated}


def get_wireguard_config(gateway: str) -> dict[str, Any]:
	"""Return the connection bundle a customer needs for one gateway."""
	server = active_gateway(gateway)
	virtual_machine = frappe.get_doc("Virtual Machine", server.virtual_machine)
	if not virtual_machine.ssh_host or not server.gateway_public_key:
		frappe.throw(_("WireGuard Gateway Server {0} has no connection bundle yet.").format(gateway))
	return {
		"gateway": server.name,
		"gateway_ipv4": virtual_machine.ssh_host,
		"listen_port": server.listen_port,
		"public_key": server.gateway_public_key,
		"region_id": frappe.get_single("Atlas Settings").region_id,
	}
