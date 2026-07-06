"""The VPN Peer — one row per customer device on the region gateway.

The design lives in `spec/25-private-networking.md` (Phase 5), the Desk surface in
`spec/26-customer-gateway-desk.md`, the full rationale in
`llm/references/customer-vpc-vpn.md`. A customer runs stock `wg-quick`, dials the
region's GATEWAY VM, and lands inside their tenant's /48 — reaching every VM in
their VPC by its stable `fdaa:` address over one tunnel.

Deliberately NOT spec/19's `VPN Tunnel`: there is no per-tunnel interface, port,
slot, or per-row server key. The gateway has ONE `wg0`, on ONE port, with ONE
public key, shared by every peer (reference §8). Every customer is one `[Peer]` on
that shared interface, enrolled by `wg set wg0 peer <pk> allowed-ips <client>/128` —
a hash-table insert, not an `ip link add`. Isolation is per-source (WireGuard
cryptokey routing pins the source to the client's own /128) plus one static
`same_48` eBPF guard confining the destination to the source's own /48, with ZERO
per-customer state.

The controller side (resolve the gateway, reconcile its wg0, push the client /128
into the host mesh) lives in `atlas/atlas/customer_gateway.py` — this file is the
DocType: computed denorms, immutability guards, and the whitelisted form actions.
"""

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas import wireguard
from atlas.atlas.networking import WG_GATEWAY_PORT, derive_client_address, derive_tenant_prefix

# The scope + the crypto identity are frozen once the row exists. The tenant is the
# isolation boundary (the client's /48), and the client public key is what the
# gateway pins the source to — neither may change under an Active peer.
IMMUTABLE_AFTER_INSERT = ("tenant", "client_public_key")


class VPNPeer(Document):
	def before_validate(self) -> None:
		# Computed denorms, refreshed like Virtual Machine.private_address. The client
		# address is a pure function of (tenant, this row's name) — so it needs the name,
		# which autoname (hash) sets before before_validate on insert. All three are
		# legibility denorms; the source of truth is the derivation.
		if not self.status:
			self.status = "Pending"
		if self.tenant:
			self.client_address = derive_client_address(self.tenant, self.name)
			self.allowed_ips = derive_tenant_prefix(self.tenant)
		if self.gateway:
			self.endpoint = self._gateway_endpoint()

	def before_insert(self) -> None:
		# Resolve the region gateway (the operator does not pick it) and validate the
		# client key BEFORE the row exists, so a malformed key or a missing gateway fails
		# loud in the controller, never on the host.
		from atlas.atlas.customer_gateway import resolve_region_gateway

		if not wireguard.is_valid_public_key(self.client_public_key or ""):
			frappe.throw(_("client_public_key is not a valid WireGuard public key"))
		if not self.gateway:
			self.gateway = resolve_region_gateway()

	def validate(self) -> None:
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			old_value = getattr(original, field)
			if old_value and old_value != getattr(self, field):
				frappe.throw(_("{0} is immutable after insert").format(field))

	def _gateway_endpoint(self) -> str:
		"""`<gateway reserved public v4>:51820` — what the customer dials. The gateway's
		fixed reserved IPv4 (like the proxy's) so the Endpoint never moves across a
		rebuild. Fails loud if the gateway has no public v4 yet (not fully stood up)."""
		address = frappe.db.get_value("Virtual Machine", self.gateway, "public_ipv4")
		if not address:
			frappe.throw(_("Gateway {0} has no public IPv4 yet").format(self.gateway))
		return f"{address}:{WG_GATEWAY_PORT}"

	@frappe.whitelist()
	def re_enroll(self) -> str:
		"""(Re-)apply this peer on the gateway and (re-)advertise its /128 into the mesh,
		then mark Active. Idempotent — the same path request_vpc_access runs, safe to
		re-run after a gateway rebuild or a partial reconcile. Raises (leaving the row as
		it was) if the gateway can't be reached, so the row only goes Active once the
		gateway actually carries the peer."""
		if self.status == "Revoked":
			frappe.throw(_("Cannot re-enroll a revoked peer; create a new one"))
		from atlas.atlas.customer_gateway import enroll_peer

		enroll_peer(self)
		return self.name

	@frappe.whitelist()
	def client_config(self) -> dict:
		"""The ready-to-use client payload (the copy-paste `.conf` + setup steps). Only
		meaningful once the gateway carries the peer and its key is denormed, so it is
		guarded on Active."""
		if self.status != "Active":
			frappe.throw(_("Client config is only available once the peer is Active"))
		from atlas.atlas.customer_gateway import client_config_payload

		return client_config_payload(self)

	@frappe.whitelist()
	def revoke(self) -> str:
		"""Drop the peer from the gateway's wg0 and withdraw its /128 from the mesh, then
		mark Revoked. The customer loses access immediately. Losing the last VM does NOT
		auto-revoke — a customer may hold a tunnel to an empty VPC."""
		if self.status == "Revoked":
			frappe.throw(_("Peer is already revoked"))
		from atlas.atlas.customer_gateway import revoke_peer

		revoke_peer(self)
		return self.name
