"""Host-side 1:1 NAT for an attached public IPv4 (a Reserved IP).

This is the *inbound* mirror of the NAT44 egress masquerade in
`vm-network-up.py`, and the shape is forced by how DigitalOcean delivers a
reserved IP — proven on a live droplet, not assumed:

A VM's public IPv6 is genuinely *routed* to the host (DO advertises the /64, the
host claims a /128 with proxy-NDP, routes it to the guest, which binds the real
address — no translation). A reserved **IPv4 is different**: DO binds it to the
droplet via an **anchor IP** — a second private address on the droplet's eth0
(e.g. 10.47.0.10/16), with an anchor gateway (e.g. 10.47.0.1). DO's edge does the
reserved↔anchor mapping inside its own fabric, so:

- **Inbound:** a packet for the reserved IP arrives at the droplet with
  **destination = the anchor IP**, NOT the reserved IP (the reserved IP never
  appears on the droplet at all). So the DNAT must match the **anchor IP**.
  (Matching the reserved IP silently never fires — the packet is delivered to the
  host's own services instead; that was the original bug this module had.)
- **Outbound:** for the guest's egress to be seen as the reserved IP, it must
  reach DO's edge **sourced from the anchor IP and routed via the anchor
  gateway** (DO's documented "outbound over a reserved IP" recipe). So we SNAT the
  guest's private v4 to the **anchor IP** and policy-route that guest's traffic
  out the **anchor gateway** — scoped to this one guest so the host's own egress
  and every other VM's NAT44 are untouched.

The reserved IP stays the public identity (recorded in Frappe, denormalized onto
the VM, published in DNS) — but on the droplet the operative handle is the
**anchor**. The guest contract is unchanged: it still sees only its private
100.64.x.x/30. See spec/06-networking.md and the `atlas-reserved-ip-anchor-dnat`
finding.

The anchor (address + gateway) is **not** in Frappe state and is not derivable —
it is per-droplet and must be discovered on the host from DO metadata
(`discover_reserved_ip_anchor()`), then threaded into the rules.

Everything here is pure string/argv construction except `apply_reserved_ip_nat()`
/ `remove_reserved_ip_nat()` / `discover_reserved_ip_anchor()`, which touch the
host. The rule *generation* is unit-testable with bare `python3 -m unittest` (no
host), like the rest of this package.
"""

from __future__ import annotations

from dataclasses import dataclass

from atlas._run import _substitute, run, run_ok

TABLE = ("inet", "atlas")
PREROUTING = "prerouting"
POSTROUTING = "postrouting"
FORWARD = "forward"

# Single policy-routing table for reserved-IP egress. One reserved IP per host
# today (one proxy VM attaches one v4), so one table suffices; the `from
# <guest-v4>` rule scopes it to that guest. Revisit if a host ever hosts two
# reserved IPs (the anchor is shared, so L3 can't separate them anyway — see the
# module docstring / spec).
EGRESS_TABLE = "atlas-reserved"
EGRESS_TABLE_ID = "100"

# DO metadata endpoints for the droplet's anchor IP (the on-droplet handle for a
# reserved IP). Stable, link-local, no DNS.
_METADATA = "http://169.254.169.254/metadata/v1/interfaces/public/0/anchor_ipv4"


@dataclass(frozen=True)
class Anchor:
	"""The droplet's anchor IPv4 — DO's on-droplet handle for any reserved IP
	bound to it. `address` is what inbound packets are destined to and what
	egress is SNAT'd to; `gateway` is what egress must route via for DO's edge to
	map anchor→reserved."""

	address: str
	gateway: str


def discover_reserved_ip_anchor() -> Anchor | None:
	"""Read the droplet's anchor IPv4 (address + gateway) from DO metadata, or
	return None when there is no anchor (a host with no DO metadata service).

	The anchor is per-droplet, not in Frappe, not derivable — so it is discovered
	here on the host at attach time and written into network.env so a cold-boot
	reconcile re-applies the same rules. DigitalOcean delivers a reserved IP via an
	anchor; a Self-Managed host (e.g. a Scaleway Elastic Metal box) is handed a
	**routed** flexible IP instead — packets arrive with destination = the reserved
	IP itself, no anchor — so metadata is absent and the caller falls back to the
	routed path (`apply_routed_reserved_ip_nat`). Returning None (not raising) is
	what lets the one `vm-reserved-ip` script serve both delivery models."""
	if not run_ok("curl -s --max-time 3 -o /dev/null {}", f"{_METADATA}/address"):
		return None
	address = run("curl -s --max-time 5 {}", f"{_METADATA}/address", check=True).strip()
	gateway = run("curl -s --max-time 5 {}", f"{_METADATA}/gateway", check=True).strip()
	if not address or not gateway:
		return None
	return Anchor(address=address, gateway=gateway)


def prerouting_chain_command() -> str:
	"""`nft add chain` for the dstnat prerouting chain (created on demand — the
	scaffold only makes `forward` + the srcnat `postrouting`; inbound DNAT is the
	first thing that needs a prerouting nat hook). The brace clause goes through a
	`{}` hole so it reaches nft as ONE argv token (Trap 2)."""
	return _substitute(
		f"add chain inet atlas {PREROUTING} {{}}",
		("{ type nat hook prerouting priority dstnat; policy accept; }",),
	)


def dnat_rule_command(anchor_ipv4: str, guest_ipv4: str) -> str:
	"""prerouting DNAT: rewrite the ANCHOR IP (the destination DO actually
	delivers a reserved-IP packet to) to the guest's private /30 address, so
	routing then carries it across the veth into the namespace and out the tap. No
	input-interface match — DO delivers to the anchor on eth0 and we don't pin the
	iif."""
	return _substitute(
		f"add rule inet atlas {PREROUTING} ip daddr {{}} dnat to {{}}",
		(anchor_ipv4, guest_ipv4),
	)


def snat_rule_command(anchor_ipv4: str, guest_ipv4: str) -> str:
	"""postrouting SNAT, **inserted at the chain head** so it is evaluated before
	the host-wide 100.64.0.0/16 masquerade: this guest's egress is sourced as the
	ANCHOR IP. Combined with the egress policy route (out the anchor gateway), DO's
	edge then maps the anchor to the reserved IP, so the world sees the reserved
	IP. Stamping the reserved IP directly here would NOT work — DO only maps
	anchor-sourced, anchor-gateway-routed traffic."""
	return _substitute(
		f"insert rule inet atlas {POSTROUTING} ip saddr {{}} snat to {{}}",
		(guest_ipv4, anchor_ipv4),
	)


def forward_rule_command(guest_ipv4: str, host_veth: str) -> str:
	"""Accept the inbound (post-DNAT) flow toward the guest's private v4 out the
	host-side veth. Today the forward chain is `policy accept`, so this is
	belt-and-suspenders — but it keeps the inbound v4 path explicit and survives a
	future per-VM firewall that flips the policy to drop (the §2.1 release gate:
	a per-VM firewall must not silently drop this hop)."""
	return _substitute(
		f"add rule inet atlas {FORWARD} ip daddr {{}} oifname {{}} accept",
		(guest_ipv4, host_veth),
	)


def apply_reserved_ip_nat(anchor: Anchor, guest_ipv4: str, host_veth: str) -> None:
	"""Idempotently install the inbound DNAT, the egress SNAT, the forward accept,
	and the egress policy route. Re-running (cold boot, reconcile, double attach)
	is a no-op — the same self-healing contract as the rest of vm-network-up.py.

	Idempotency is by substring match against the live ruleset rather than handle
	tracking: the match keys (the anchor IP and the guest /30 address) are unique
	per guest, so the lines are an exact enough fingerprint and it survives nft
	re-rendering the rule text."""
	if not run_ok("sudo nft list chain {} {} {}", *TABLE, PREROUTING):
		run("sudo nft " + prerouting_chain_command())

	prerouting = run("sudo nft list chain {} {} {}", *TABLE, PREROUTING)
	if not _has_dnat(prerouting, anchor.address, guest_ipv4):
		run("sudo nft " + dnat_rule_command(anchor.address, guest_ipv4))

	postrouting = run("sudo nft list chain {} {} {}", *TABLE, POSTROUTING)
	if not _has_snat(postrouting, anchor.address, guest_ipv4):
		run("sudo nft " + snat_rule_command(anchor.address, guest_ipv4))

	forward = run("sudo nft list chain {} {} {}", *TABLE, FORWARD)
	if not _has_forward(forward, guest_ipv4, host_veth):
		run("sudo nft " + forward_rule_command(guest_ipv4, host_veth))

	_apply_egress_route(anchor, guest_ipv4)


def apply_routed_reserved_ip_nat(reserved_ipv4: str, guest_ipv4: str, host_veth: str) -> None:
	"""Idempotently install the inbound DNAT, the egress SNAT, and the forward
	accept for a **routed** reserved IP (the Self-Managed / Scaleway-Elastic-Metal
	model), where there is no anchor.

	Unlike the DO anchor path, the vendor routes the flexible IP straight to the
	host's uplink, so inbound packets arrive with destination = the reserved IP
	itself: DNAT *that* to the guest's /30. Egress is symmetric and needs no policy
	route — the guest's traffic leaves over the host's normal default route, SNAT'd
	to the reserved IP, and the vendor accepts it because the IP is genuinely routed
	to this host (proven: pinging the IP reaches the host once it owns the route).
	The reserved IP must NOT be a local address on the host (no `ip addr add`):
	the prerouting DNAT fires before the input/forward decision, so leaving it
	off-link is what lets the packet be forwarded to the guest instead of consumed
	by the host's own services.

	Same substring-match idempotency as `apply_reserved_ip_nat`; the reserved IP and
	the guest /30 are unique per guest, so a re-run (cold boot, reconcile, double
	attach) is a no-op. `remove_reserved_ip_nat` (keyed on the guest v4) tears both
	models down — the routed path simply has no egress policy route to drop."""
	if not run_ok("sudo nft list chain {} {} {}", *TABLE, PREROUTING):
		run("sudo nft " + prerouting_chain_command())

	prerouting = run("sudo nft list chain {} {} {}", *TABLE, PREROUTING)
	if not _has_dnat(prerouting, reserved_ipv4, guest_ipv4):
		run("sudo nft " + dnat_rule_command(reserved_ipv4, guest_ipv4))

	postrouting = run("sudo nft list chain {} {} {}", *TABLE, POSTROUTING)
	if not _has_snat(postrouting, reserved_ipv4, guest_ipv4):
		run("sudo nft " + snat_rule_command(reserved_ipv4, guest_ipv4))

	forward = run("sudo nft list chain {} {} {}", *TABLE, FORWARD)
	if not _has_forward(forward, guest_ipv4, host_veth):
		run("sudo nft " + forward_rule_command(guest_ipv4, host_veth))


def remove_reserved_ip_nat(guest_ipv4: str) -> None:
	"""Delete the three rules by handle and the egress policy route, best-effort.
	Keyed on the guest's private v4 (the one match common to all three rules and
	the policy rule), so the anchor IP isn't needed at teardown — a detach can run
	without re-discovering metadata. Symmetric with the down path in
	vm-network-down.py: missing rules / a missing chain are not an error (the
	detach may run after the VM is already torn down). The shared masquerade and
	the prerouting chain itself are left in place — like the host-wide scaffold,
	they cost nothing and serve the next attach."""
	for chain in (PREROUTING, POSTROUTING, FORWARD):
		listing = run("sudo nft -a list chain {} {} {}", *TABLE, chain, check=False)
		for handle in _handles_for(listing, guest_ipv4):
			run("sudo nft delete rule inet atlas {} handle {}", chain, handle, check=False)
	_remove_egress_route(guest_ipv4)


def _apply_egress_route(anchor: Anchor, guest_ipv4: str) -> None:
	"""Policy-route this guest's egress out the anchor gateway so DO maps it to the
	reserved IP. Scoped to `from <guest-v4>` so the host's own default route and
	every other VM's NAT44 egress are untouched. Idempotent (`replace`/re-add)."""
	uplink = _uplink_device()
	# A dedicated table whose default route is the anchor gateway. `route replace`
	# is idempotent. The anchor /16 is on-link on the uplink, so the gateway is
	# reachable; scope link is not needed for the default route via a gateway.
	run(
		"sudo ip -4 route replace default via {} dev {} table {}",
		anchor.gateway, uplink, EGRESS_TABLE_ID,
	)  # fmt: skip
	# The rule: packets sourced from the guest's private v4 consult that table
	# BEFORE main. Add only if absent (ip rule has no `replace`).
	rules = run("sudo ip -4 rule show")
	if f"from {guest_ipv4} " not in rules and f"from {guest_ipv4}\t" not in rules:
		run("sudo ip -4 rule add from {} lookup {}", guest_ipv4, EGRESS_TABLE_ID)


def _remove_egress_route(guest_ipv4: str) -> None:
	"""Drop the policy rule for this guest (best-effort). The table's default route
	is left — it is shared scaffolding keyed only by the rule, harmless when no
	rule points at it, and re-`replace`d on the next attach."""
	run("sudo ip -4 rule del from {} lookup {}", guest_ipv4, EGRESS_TABLE_ID, check=False)


def _uplink_device() -> str:
	"""The v4 uplink (default-route dev) — where the anchor gateway is reachable.
	Imported lazily to keep this module importable without the network_env helper
	in pure unit tests."""
	from atlas.network_env import default_route_device

	return default_route_device()


def _has_dnat(listing: str, anchor_ipv4: str, guest_ipv4: str) -> bool:
	return any(anchor_ipv4 in line and guest_ipv4 in line and "dnat" in line for line in listing.splitlines())


def _has_snat(listing: str, anchor_ipv4: str, guest_ipv4: str) -> bool:
	return any(anchor_ipv4 in line and guest_ipv4 in line and "snat" in line for line in listing.splitlines())


def _has_forward(listing: str, guest_ipv4: str, host_veth: str) -> bool:
	return any(guest_ipv4 in line and host_veth in line for line in listing.splitlines())


def _handles_for(listing: str, guest_ipv4: str):
	"""Trailing handle number of every rule mentioning this guest's v4 (the one
	match key common to all three rules). `nft -a` prints `... # handle N`; the
	handle is the last token. Mirrors the handle-scrape in vm-network-down.py."""
	for line in listing.splitlines():
		if guest_ipv4 in line and "handle" in line:
			yield line.split()[-1]
