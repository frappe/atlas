"""The VPN-broker API — a VM owner (or Central) asks Atlas for a tunnel.

The user-facing write half of the broker (spec/19-vpn-broker.md). Like
`provision.create_vm`, it is owner-scoped and Central-callable as the service
user: the caller must be an operator or own the target VM. The client mints its
own keypair and sends only its public key; the response carries everything to
assemble a ready WireGuard config (the host public key, the endpoint, the
AllowedIPs scoped to the one VM, the assigned overlay address) plus a copy-paste
template and setup steps.
"""

from __future__ import annotations

import frappe
from frappe import _

from atlas.atlas.networking import tunnel_endpoint_address
from atlas.atlas.permissions import _is_operator
from atlas.atlas.wireguard import is_valid_public_key

_INSTRUCTIONS = (
	"1. Generate a keypair (the private key stays on your machine):\n"
	"     wg genkey | tee privatekey | wg pubkey > publickey\n"
	"2. Request the tunnel with the PUBLIC key (this call).\n"
	"3. Paste the returned `config` into /etc/wireguard/atlas.conf and fill in\n"
	"   PrivateKey with the contents of `privatekey`.\n"
	"4. Bring it up and reach the VM at its IPv6:\n"
	"     wg-quick up atlas && ssh root@<vm-ipv6>"
)


@frappe.whitelist()
def request_tunnel(virtual_machine: str, client_public_key: str, label: str | None = None) -> dict:
	"""Provision a WireGuard tunnel to `virtual_machine` for the caller's client key.

	Validates the client key, authorizes access to the VM, creates the `VPN Tunnel`
	row (allocating its slot/port/overlay), brings it up on the host (which mints
	the host key and returns its public half), and returns the client config. Runs
	with `ignore_permissions` after the explicit access check — operator
	orchestration authorized by ownership, the `provision.create_vm` pattern."""
	if not is_valid_public_key(client_public_key):
		frappe.throw(_("client_public_key is not a valid WireGuard public key"))
	_assert_can_access_vm(virtual_machine)

	tunnel = frappe.get_doc(
		{
			"doctype": "VPN Tunnel",
			"virtual_machine": virtual_machine,
			"client_public_key": client_public_key,
			"label": label or "",
		}
	).insert(ignore_permissions=True)
	tunnel.bring_up()
	return _client_config(tunnel)


def _assert_can_access_vm(virtual_machine: str) -> None:
	"""Operator, or the VM's owner. Mirrors the SPA's owner scoping
	(`permissions.owner_only`) and lets Central through as the service user that
	owns the VMs it created."""
	if not frappe.db.exists("Virtual Machine", virtual_machine):
		frappe.throw(_("Virtual Machine {0} not found").format(virtual_machine))
	user = frappe.session.user
	if _is_operator(user):
		return
	if frappe.db.get_value("Virtual Machine", virtual_machine, "owner") != user:
		raise frappe.PermissionError(_("You do not have access to this Virtual Machine"))


def _client_config(tunnel) -> dict:
	"""The ready-to-use client payload: the host public key, the endpoint, the
	AllowedIPs scoped to the one VM, the client's overlay address, a copy-paste
	`config`, and setup steps."""
	virtual_machine_ipv6 = frappe.db.get_value("Virtual Machine", tunnel.virtual_machine, "ipv6_address")
	endpoint = f"{tunnel_endpoint_address(tunnel.server)}:{tunnel.listen_port}"
	allowed_ips = f"{virtual_machine_ipv6}/128"
	client_address = f"{tunnel.client_address}/128"
	return {
		"name": tunnel.name,
		"interface": tunnel.interface_name,
		"server_public_key": tunnel.server_public_key,
		"endpoint": endpoint,
		"allowed_ips": allowed_ips,
		"client_address": client_address,
		"config": _render_config(tunnel.server_public_key, endpoint, allowed_ips, client_address),
		"instructions": _INSTRUCTIONS,
	}


def _render_config(server_public_key: str, endpoint: str, allowed_ips: str, client_address: str) -> str:
	"""A WireGuard .conf with a PrivateKey placeholder — the client fills in its own
	private key (Atlas never sees it). PersistentKeepalive keeps the path open for a
	client behind NAT (common for a v4 client)."""
	return (
		"[Interface]\n"
		"PrivateKey = <your client private key>\n"
		f"Address = {client_address}\n"
		"\n"
		"[Peer]\n"
		f"PublicKey = {server_public_key}\n"
		f"Endpoint = {endpoint}\n"
		f"AllowedIPs = {allowed_ips}\n"
		"PersistentKeepalive = 25\n"
	)
