"""Host-side private-plane isolation rules for the WireGuard mesh (design §4).

Each VM has its own netns + veth, so its source is physically attributable at the
veth — the shared-fabric "must *infer* whether the source is forged" problem does
not arise, and **nftables suffices; eBPF is rejected** (design §4). All rules live
in the existing host root-ns `inet atlas` FORWARD chain, added by `vm-network-up.py`
as a pure function of the VM's own row.

The plane is fail-closed: a single TERMINAL guard (`ip6 daddr fdaa::/16 drop`,
installed once at scaffold time by bootstrap-server.py and re-asserted here) makes
the private plane default-deny WITHOUT flipping the whole chain's policy. Every
per-VM rule below is allow-by-exception, `nft insert`ed (head) ABOVE that terminal
drop. The four per-VM rules (design §4b):

  1. ANTI-SPOOF — a packet INTO the mesh from this guest's veth MUST carry exactly
     this VM's /128 source (catches a forged cross-tenant OR infra source in one).
  2. SAME-TENANT egress — this VM may reach its own /48.
  3. INFRA-DESTINATION — this VM may reach (and reply to) the proxy/resolver in the
     infra /48; without it the terminal drop black-holes every proxied reply.
  4. CROSS-HOST DELIVERY — a packet decap'd from a peer host (iifname wg-mesh) into
     this VM's veth, ACCEPTED ONLY when its source is in this VM's own tenant /48. This
     folds in design §4b RULE 5 (the cross-tenant drop on wg-mesh ingress): AllowedIPs
     pins a decap'd packet's HOST, not its tenant, so without the `saddr $t48` constraint
     a peer host could deliver a cross-tenant inner source straight into this veth. With
     it, a cross-tenant mesh-ingress packet matches no accept and falls to the terminal
     drop. Proven necessary by a real two-host e2e (a tenant-B VM reached a tenant-A VM
     across the mesh until this constraint was added).

Because rules 2/3 REQUIRE `ip6 saddr $priv` and rule 1 fires on `saddr != $priv`, the
accept and anti-spoof rules PARTITION the source space — a spoofed egress packet can
never match an accept, so the `nft insert` head-ordering (which lands them
[4,3,2,1,terminal-drop]) is sound regardless of the numbering. Verified against
canonical `nft list` output on a real host.

The rule TEXT and the idempotency GUARDS are canonicalized to match `nft list`'s
output exactly (e.g. the infra `fdaa:0:0::/48` prints as `fdaa::/48`), so a re-run
does not duplicate rules. The command builders use `_substitute` so interpolated
values are auto-quoted while the nft keywords stay separate argv tokens — the exact
idiom `wireguard.py` uses. Everything here is pure string construction except
`apply_private_network` / `remove_private_network`.
"""

from __future__ import annotations

from atlas._run import _substitute, run

# The whole private plane. The terminal drop keys on this; egress anti-spoof too.
PRIVATE_PLANE = "fdaa::/16"
# The reserved infra /48 (proxy/resolver). nft canonicalizes fdaa:0:0::/48 -> fdaa::/48.
INFRA_PREFIX_CANONICAL = "fdaa::/48"
MESH_DEVICE = "wg-mesh"


# --- rule TEXT as it appears in `nft list` (the idempotency guards match this) ------


def terminal_drop_text() -> str:
	"""The private-plane default-deny (design §4a), as `nft list` prints it."""
	return f"ip6 daddr {PRIVATE_PLANE} drop"


def anti_spoof_text(veth: str, private_address: str) -> str:
	return f'iifname "{veth}" ip6 daddr {PRIVATE_PLANE} ip6 saddr != {private_address} drop'


def same_tenant_egress_text(veth: str, private_address: str, tenant_prefix: str) -> str:
	return f'iifname "{veth}" ip6 saddr {private_address} ip6 daddr {tenant_prefix} accept'


def infra_destination_text(veth: str, private_address: str) -> str:
	return f'iifname "{veth}" ip6 saddr {private_address} ip6 daddr {INFRA_PREFIX_CANONICAL} accept'


def cross_host_delivery_text(veth: str, private_address: str, tenant_prefix: str) -> str:
	"""Rule 4 + the design's rule 5 folded in: a packet decap'd from a peer host into
	this VM's veth is accepted ONLY when its source is in this VM's OWN tenant /48. This
	is load-bearing for isolation: AllowedIPs pins a decap'd packet's HOST, not its
	tenant, so a compromised (or merely co-tenant-confused) peer could otherwise forge a
	cross-tenant inner source and reach this VM. Constraining the source to `$t48` here —
	instead of a bare `daddr $priv accept` — makes a cross-tenant mesh-ingress packet fall
	through to the terminal `fdaa::/16 drop` (design §4b rule 5). Same-tenant cross-host
	traffic still matches, so legitimate reach is preserved."""
	return (
		f'iifname "{MESH_DEVICE}" oifname "{veth}" '
		f"ip6 saddr {tenant_prefix} ip6 daddr {private_address} accept"
	)


def per_vm_texts(veth: str, private_address: str, tenant_prefix: str) -> list[str]:
	"""The four per-VM rule bodies as they appear in `nft list`, in design order.
	They are `nft insert`ed (head) so the chain ends up [4,3,2,1,terminal-drop];
	because the accepts require `saddr $priv` (or, for rule 4, `saddr $t48`) and rule 1
	fires on `saddr != $priv`, the head ordering is sound (the sources partition)."""
	return [
		anti_spoof_text(veth, private_address),
		same_tenant_egress_text(veth, private_address, tenant_prefix),
		infra_destination_text(veth, private_address),
		cross_host_delivery_text(veth, private_address, tenant_prefix),
	]


# --- nft command builders (values auto-quoted via _substitute; keywords stay tokens)-


def _terminal_drop_command() -> str:
	# `add` (tail) so the terminal drop stays LAST, below every inserted per-VM allow.
	return _substitute("add rule inet atlas forward ip6 daddr {} drop", (PRIVATE_PLANE,))


def _anti_spoof_command(veth: str, private_address: str) -> str:
	return _substitute(
		"insert rule inet atlas forward iifname {} ip6 daddr {} ip6 saddr != {} drop",
		(veth, PRIVATE_PLANE, private_address),
	)


def _same_tenant_egress_command(veth: str, private_address: str, tenant_prefix: str) -> str:
	return _substitute(
		"insert rule inet atlas forward iifname {} ip6 saddr {} ip6 daddr {} accept",
		(veth, private_address, tenant_prefix),
	)


def _infra_destination_command(veth: str, private_address: str) -> str:
	return _substitute(
		"insert rule inet atlas forward iifname {} ip6 saddr {} ip6 daddr {} accept",
		(veth, private_address, "fdaa:0:0::/48"),
	)


def _cross_host_delivery_command(veth: str, private_address: str, tenant_prefix: str) -> str:
	# Accept a mesh-decap'd packet into this veth ONLY from the VM's own tenant /48 —
	# design §4b rule 5, so a cross-tenant inner source falls to the terminal drop.
	return _substitute(
		"insert rule inet atlas forward iifname {} oifname {} ip6 saddr {} ip6 daddr {} accept",
		(MESH_DEVICE, veth, tenant_prefix, private_address),
	)


def _per_vm_commands(veth: str, private_address: str, tenant_prefix: str) -> list[str]:
	return [
		_anti_spoof_command(veth, private_address),
		_same_tenant_egress_command(veth, private_address, tenant_prefix),
		_infra_destination_command(veth, private_address),
		_cross_host_delivery_command(veth, private_address, tenant_prefix),
	]


# --- apply / remove (the only host-touching functions) ------------------------------


def apply_private_network(veth: str, private_address: str, tenant_prefix: str) -> None:
	"""Install this VM's private-plane isolation rules. Idempotent: each rule is
	`nft insert`ed only if its canonical text is absent from the live chain, so a
	re-run (cold boot, restart) is a no-op — the vm-network-up.py / tunnel self-healing
	contract.

	The terminal drop is (re-)asserted first with `add` (tail) so the plane is
	default-deny even on a host whose scaffold predates the feature; then the four
	per-VM allow rules are `insert`ed (head), landing ABOVE it. The guard for each is
	the exact `nft list` text (per_vm_texts / terminal_drop_text)."""
	listing = run("sudo nft list chain inet atlas forward", check=False)
	if terminal_drop_text() not in listing:
		run("sudo nft " + _terminal_drop_command())
	# Re-read once (the add above changed the chain); the per-VM guards compare the
	# same canonical text.
	listing = run("sudo nft list chain inet atlas forward", check=False)
	texts = per_vm_texts(veth, private_address, tenant_prefix)
	commands = _per_vm_commands(veth, private_address, tenant_prefix)
	for text, command in zip(texts, commands, strict=True):
		if text not in listing:
			run("sudo nft " + command)


def remove_private_network(private_address: str, veth: str) -> None:
	"""Delete this VM's per-VM rules by handle — every forward rule mentioning EITHER
	the VM's private /128 OR its veth (rule 4 is veth-keyed with the /128 too; rules
	1-3 carry the /128; the terminal drop mentions neither, so it is left in place).
	Best-effort + idempotent, symmetric with vm-network-down.py."""
	listing = run("sudo nft -a list chain inet atlas forward", check=False)
	for handle in _handles_for(listing, private_address, veth):
		run("sudo nft delete rule inet atlas forward handle {}", handle, check=False)


def _handles_for(listing: str, private_address: str, veth: str):
	"""Trailing handle of every forward rule mentioning this VM's /128 or its veth.
	`nft -a` prints `… # handle N`. The host-wide terminal drop mentions neither a
	specific /128 nor the veth, so it is naturally excluded."""
	for line in listing.splitlines():
		if "handle" not in line:
			continue
		if private_address in line or f'"{veth}"' in line:
			yield line.split()[-1]
