"""Render the canonical `wg-mesh` config from the effective tables (spec §16.2).

Pure string function: given the host's own Membership Record + the effective
Membership and Ownership tables, produce the `wg-mesh.conf` body the apply
pipeline (§16.4) writes to disk and feeds to `wg syncconf`. Byte-canonical —
peers sorted by pubkey, AllowedIPs sorted — so the "in sync?" check is a plain
string compare. This is the `atlas-networkd` analogue of
`atlas/atlas/host_mesh.py:render_wg_mesh_config` and emits the SAME canonical
shape, so a host migrated from the controller-side path to ANCP reads as "in
sync" on the first render against a config the controller last pushed.

Non-overlap invariant (spec §16.3 — Issue B): within a single rendered config,
no /128 appears in the AllowedIPs of more than one peer. Guaranteed two ways:

  1. Each /128 in `OwnershipTable.owner_of` has exactly one owner, so it lands
     in exactly one peer's AllowedIPs. A /128 in `conflicts` is dropped
     entirely (no peer advertises it) — the safe default on a §7.3 conflict.
  2. The render is a whole-table recompute; the apply pipeline (§16.4) applies
     it as a single atomic `wg syncconf`, never incrementally per-peer.

We DO NOT exclude peers based on observer-local suspicion (`suspect` peers stay
routed — spec §14 keeps routing to a partitioned host's VMs intact during the
suspicion window; only `dead`-past-`dead_grace` records are removed upstream,
so they never reach this render).
"""

from __future__ import annotations

from .records import MembershipRecord, OwnershipTable

# The config carries ListenPort only — the PrivateKey lives in its own 0600
# file (never in this body), exactly like the existing `render_wg_mesh_config`.
# The apply pipeline (commands.apply_script) sets the key LAST after syncconf
# (load-bearing; see its docstring).
_WG_HOST_PORT = 51820
_KEEPALIVE_SECONDS = 25


def render_wg_desired(
	self_host_id: str,
	members: dict[str, MembershipRecord],
	ownership: OwnershipTable,
	*,
	wg_host_port: int = _WG_HOST_PORT,
) -> str:
	"""The canonical `wg-mesh.conf` body for `self_host_id` (spec §16.2).

	`members` keys are HostID, values the latest Membership Record per origin.
	One [Peer] per OTHER member: AllowedIPs is the sorted set of /128s that
	member owns (per the effective ownership table) PLUS that member's own infra
	mesh /128 (so the host↔host bus can dial it). Conflicting /128s (in
	`ownership.conflicts`) are dropped — never emitted to ANY peer. The host at
	`self_host_id` is skipped (a host does not peer with itself).

	`endpoint` on the Membership Record is the bare public IPv6 (no port); the
	render wraps it as `[{endpoint}]:{port}` to match the existing
	`render_wg_mesh_config` canonical bytes.

	Returns the file body with a single trailing newline; byte-canonical so a
	string compare against the last-applied config detects drift.
	"""
	# Per-peer /128-set precomputed from the effective table. A /128 in
	# `ownership.owner_of` lands in exactly one peer's set (the non-overlap
	# invariant, §16.3); a /128 in `ownership.conflicts` is in neither
	# `owner_of` (by construction) nor any peer's set — it's dropped.
	allowed_by_peer: dict[str, list[str]] = {host_id: [] for host_id in members}
	for ip, owner in ownership.owner_of.items():
		# An owner not in `members` (removed/gc'd upstream) → no peer advertises
		# the /128 this round; anti-entropy / GC will reconcile it. Skip, do not
		# invent a peer.
		if owner in allowed_by_peer:
			allowed_by_peer[owner].append(f"{ip}/128")
	_assert_no_input_overlap(allowed_by_peer, ownership)

	lines = [
		"[Interface]",
		f"ListenPort = {wg_host_port}",
		"",
	]
	# Sort by pubkey for byte-canonical output, the same key the existing
	# render uses — so a config the controller last pushed byte-compares against
	# an ANCP-rendered one when in the same state.
	for peer in sorted(members.values(), key=lambda m: m.wg_public_key):
		if peer.host_id == self_host_id:
			# A host never peers with itself; if a record carrying our own host_id
			# somehow reaches the render (e.g. our own self-record is in the
			# table), skip it rather than emit a self-[Peer].
			continue
		if not peer.wg_public_key:
			# wg_public_key is unknown (seed entry before ANCP handshake carries
			# "" because the host self-generates its key on first boot).  Skip
			# until the real key arrives via gossip/anti-entropy.
			continue
		# Belt-and-suspenders: belt = the parse boundary (wire.membership_from_dict
		# + seed._seed_entry_to_record) already calls `peer.validate()` and
		# rejects whitespace/control chars in any of the three fields
		# interpolated below. Suspenders: call it AGAIN at the render doorstep
		# so a future code path that constructs a `MembershipRecord` directly
		# (tests, a new apply entry, an in-place mutation) cannot emit newline-
		# injected `[Peer]` directives into wg-mesh.conf. The config body is
		# fed to `wg syncconf` via `wg-quick strip`, which preserves all
		# `[Peer]` sections — a newline in `wg_public_key`/`endpoint`/`mesh_address`
		# would inject a rogue peer with an attacker-controlled pubkey (whose
		# priv mate the attacker actually holds, unlike the §19.2 self-forgery
		# case where they don't hold the priv mate of the cluster's trusted wg
		# key). The check is ~3 µs/peer; the loop runs every 200 ms.
		peer.validate()
		allowed = sorted([*allowed_by_peer.get(peer.host_id, []), f"{peer.mesh_address}/128"])
		lines += [
			"[Peer]",
			f"PublicKey = {peer.wg_public_key}",
			f"AllowedIPs = {', '.join(allowed)}",
			f"Endpoint = [{peer.endpoint}]:{wg_host_port}",
			f"PersistentKeepalive = {_KEEPALIVE_SECONDS}",
			"",
		]
	return "\n".join(lines) + "\n"


def _assert_no_input_overlap(allowed_by_peer: dict[str, list[str]], ownership: OwnershipTable) -> None:
	"""Self-test hook proving the §16.3 invariant holds at the render input: a
	/128 in `ownership.owner_of` is in exactly one peer's accumulator. Catches
	a future bug in the effective-table computation before it reaches the wire.
	"""
	# Every IP placed should be unique across the per-peer accumulators (one
	# owner per /128 per `owner_of`).
	all_placed: dict[str, str] = {}
	for host_id, ips in allowed_by_peer.items():
		for ip in ips:
			prior = all_placed.get(ip)
			assert prior is None, f"render input overlap: {ip} placed for both {prior} and {host_id}"
			all_placed[ip] = host_id
	# And `conflicts` must NOT appear in `owner_of` at all (the effective-table
	# rule), else we'd silently route a conflicting /128.
	assert ownership.conflicts.isdisjoint(ownership.owner_of.keys()), (
		f"conflicting /128s leaked into owner_of: {ownership.conflicts & set(ownership.owner_of)}"
	)


__all__ = ["render_wg_desired"]
