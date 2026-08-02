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
no /128 appears in the AllowedIPs of more than one peer. Guaranteed three ways:

  1. Each /128 in `OwnershipTable.owner_of` has exactly one owner, so it lands
     in exactly one peer's AllowedIPs. A /128 in `conflicts` is dropped
     entirely (no peer advertises it) — the safe default on a §7.3 conflict.
  2. Each member's own `mesh_address/128` is folded into the SAME cross-peer
     overlap accounting as owned /128s BEFORE the config is emitted — so a
     mesh_address that collides with another peer's owned /128 or mesh_address
     (a compromised/authenticated host, or an honest birthday collision) is
     dropped from ALL peers, never misdelivered.
  3. The render is a whole-table recompute; the apply pipeline (§16.4) applies
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
	"""The canonical `wg-mesh.conf` body for `self_host_id` (spec §16.2). Thin
	wrapper over `render_wg_desired_with_conflicts` that discards the render-level
	conflict map — kept for callers/tests that only want the config body."""
	body, _render_conflicts = render_wg_desired_with_conflicts(
		self_host_id, members, ownership, wg_host_port=wg_host_port
	)
	return body


def render_wg_desired_with_conflicts(
	self_host_id: str,
	members: dict[str, MembershipRecord],
	ownership: OwnershipTable,
	*,
	wg_host_port: int = _WG_HOST_PORT,
) -> tuple[str, dict[str, frozenset[str]]]:
	"""Like `render_wg_desired` but ALSO returns the render-level conflict map
	`{private_ip: origins}` — the H2 mesh_address collisions dropped in this
	render (a /128 that would have landed under >1 peer as an owned /128, a
	mesh_address, or one of each). These are NOT in `ownership.conflicts` (which
	only carries owned-/128 double-ownership); the daemon unions the two sources
	before surfacing them to the operator (spec §7.3 / §18.2 — "report loudly").
	The `origins` are the host_ids whose per-peer AllowedIPs contended for the
	/128 (i.e. the peers it was dropped from).

	The canonical `wg-mesh.conf` body for `self_host_id` (spec §16.2).

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
	# Per-peer /128-set precomputed from the effective table PLUS each member's
	# own infra mesh /128. A /128 in `ownership.owner_of` lands in exactly one
	# peer's set (the non-overlap invariant, §16.3); a /128 in
	# `ownership.conflicts` is in neither `owner_of` (by construction) nor any
	# peer's set — it's dropped.
	#
	# The `mesh_address/128` is folded into the SAME cross-peer accounting as
	# owned /128s (§16.3 / §7.3). WHY: `mesh_address` is an
	# author-controlled field on a signed Membership Record — a compromised-but-
	# authenticated host (or an honest birthday collision at ~320 hosts) can put
	# a victim tenant's /128 (or another host's mesh /128) in its `mesh_address`,
	# and only whitespace/control chars are validated (`records.validate`). If we
	# appended it AFTER the overlap check (the old bug), the same /128 could land
	# in two peers' AllowedIPs → WireGuard cryptokey-routing misdelivery. So a
	# /128 (owned OR mesh_address) that would appear under MORE THAN ONE peer is a
	# conflict → dropped from ALL peers, exactly like a §7.3 owned conflict:
	# "drop, never elect".
	allowed_by_peer: dict[str, set[str]] = {host_id: set() for host_id in members}
	for ip, owner in ownership.owner_of.items():
		# An owner not in `members` (removed/gc'd upstream) → no peer advertises
		# the /128 this round; anti-entropy / GC will reconcile it. Skip, do not
		# invent a peer.
		if owner in allowed_by_peer:
			allowed_by_peer[owner].add(f"{ip}/128")
	for host_id, peer in members.items():
		allowed_by_peer[host_id].add(f"{peer.mesh_address}/128")

	# Global cross-peer pass: any /128 that appears under more than one peer —
	# whether it got there as an owned /128, a mesh_address, or one of each — is
	# an overlap the invariant forbids. Drop it from EVERY peer (never emit an
	# overlapping /128) and record it so the collision is discoverable (a later
	# task wires operator alerting off `render_conflicts`).
	placements: dict[str, list[str]] = {}
	for host_id, ips in allowed_by_peer.items():
		for ip in ips:
			placements.setdefault(ip, []).append(host_id)
	# `render_conflict_origins` carries the contending peers per dropped /128 so
	# the daemon can surface `{private_ip, origins}` to the operator (§18.2). The
	# /128 strings carry a `/128` suffix here (the AllowedIPs form); strip it so
	# the reported `private_ip` matches the owned-conflict shape (a bare /128).
	render_conflict_origins: dict[str, frozenset[str]] = {
		ip.removesuffix("/128"): frozenset(owners) for ip, owners in placements.items() if len(owners) > 1
	}
	render_conflicts = frozenset(ip for ip, owners in placements.items() if len(owners) > 1)
	if render_conflicts:
		for ips in allowed_by_peer.values():
			ips -= render_conflicts

	# Sorted lists are the canonical per-peer AllowedIPs (owned ∪ mesh_address,
	# overlaps dropped) the render + the invariant assertion both consume.
	allowed_lists: dict[str, list[str]] = {h: sorted(ips) for h, ips in allowed_by_peer.items()}
	_assert_no_input_overlap(allowed_lists, ownership, render_conflicts)

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
		# `allowed_lists` already folds in this peer's mesh_address/128 and has
		# dropped any /128 that overlapped another peer (§16.3) — so a peer whose
		# ONLY /128 was an overlapping mesh_address renders an empty AllowedIPs
		# rather than a misdelivering one.
		allowed = allowed_lists.get(peer.host_id, [])
		lines += [
			"[Peer]",
			f"PublicKey = {peer.wg_public_key}",
			f"AllowedIPs = {', '.join(allowed)}",
			f"Endpoint = [{peer.endpoint}]:{wg_host_port}",
			f"PersistentKeepalive = {_KEEPALIVE_SECONDS}",
			"",
		]
	return "\n".join(lines) + "\n", render_conflict_origins


def _assert_no_input_overlap(
	allowed_by_peer: dict[str, list[str]],
	ownership: OwnershipTable,
	render_conflicts: frozenset[str] = frozenset(),
) -> None:
	"""Self-test hook proving the §16.3 invariant holds on the FINAL per-peer
	AllowedIPs — owned /128s AND each peer's `mesh_address/128`, with overlaps
	already dropped. No /128 may appear under more than one peer. Catches a
	future bug in the effective-table computation OR the mesh_address folding
	before it reaches the wire (a duplicate /128 would misdeliver tenant
	traffic under WireGuard cryptokey routing).
	"""
	# Every IP placed must be unique across the per-peer accumulators — this is
	# the end-to-end invariant, now including mesh_address /128s.
	all_placed: dict[str, str] = {}
	for host_id, ips in allowed_by_peer.items():
		for ip in ips:
			prior = all_placed.get(ip)
			assert prior is None, f"render input overlap: {ip} placed for both {prior} and {host_id}"
			all_placed[ip] = host_id
	# A dropped render conflict (owned-vs-mesh, mesh-vs-mesh, …) must appear in
	# ZERO peers — it was over-claimed, so we route it nowhere (§7.3 drop rule).
	leaked = render_conflicts & set(all_placed)
	assert not leaked, f"dropped render conflict leaked back into a peer: {leaked}"
	# And `conflicts` must NOT appear in `owner_of` at all (the effective-table
	# rule), else we'd silently route a conflicting /128.
	assert ownership.conflicts.isdisjoint(ownership.owner_of.keys()), (
		f"conflicting /128s leaked into owner_of: {ownership.conflicts & set(ownership.owner_of)}"
	)


__all__ = ["render_wg_desired", "render_wg_desired_with_conflicts"]
