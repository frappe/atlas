"""Persistence + the duplicate-suppression cache (spec §13.3, §14.5).

The applied-records tables + the per-host Generation counter + the seen-cache
are persisted under `/var/lib/atlas-networkd/` so a crash-restart recovers the
exact state the daemon had, without waiting for anti-entropy to refill
(spec §14.5). The effective tables are NOT persisted — they're derivable from
the per-origin latest records (spec §7.2), so we persist the inputs only.

`AppliedState` is the on-disk shape:

    {
      "membership": { host_id: MembershipRecord.as_dict(), ... },
      "ownership":  { origin:   OwnershipAdvertisement.as_dict(), ... },
      "seen":       [ [origin, kind, generation], ... ],   # bounded LRU
      "own_generation": int                                # this host's gen counter
    }

I/O is via small helpers + an atomic `os.replace` write, so a crash mid-write
leaves the previous file intact (the same idiom Task scripts use).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .records import (
	Generation,
	HostID,
	MembershipKind,
	MembershipRecord,
	MemberState,
	OwnershipAdvertisement,
	dedupe_key_membership,
	dedupe_key_ownership,
	membership_replaces,
	ownership_replaces,
)

_STATE_FILENAME = "state.json"


@dataclass(slots=True)
class AppliedState:
	"""The persisted applied-tables + seen-cache (spec §13.3, §14.5). The
	effective tables are NOT stored — derive them via
	`records.effective_ownership` + the membership dict on demand.
	"""

	membership: dict[HostID, MembershipRecord] = field(default_factory=dict)
	ownership: dict[HostID, OwnershipAdvertisement] = field(default_factory=dict)
	# §14.3 — a dead host's Membership Record, reaped from `membership` at
	# `dead_grace` but kept HERE (render-only) until its ownership is also reaped
	# at `ownership_grace`. Only `render` reads this map (via
	# `Daemon.render_current`), so the reaped host is gone from every peer-
	# selection / probe / anti-entropy path (all of which read `membership`) — it
	# is not gossiped-to, probed, or anti-entropy'd — while still carrying a
	# routable `[Peer]` for its own /128s during the late-refute window (§14.5).
	# Without this the /128s blackhole ~`ownership_grace - dead_grace` s early:
	# once membership is gone the render has no `[Peer]` to carry them even though
	# their Ownership Records deliberately outlive membership.
	routable_dead: dict[HostID, MembershipRecord] = field(default_factory=dict)
	# §19.5 — signing pubkeys learned at runtime via the TOFU introduction path
	# (`default_envelope_verifier`), keyed `HostID → signing_public_key`. These
	# are NOT in the seed and NOT (yet) in a persisted MembershipRecord, so
	# without persisting them a restart treats an introduced peer as first-
	# contact again and drops its envelopes (`signature_failed`) until it
	# re-cold-joins — a one-sided partition (M6). `main.py` merges this map into
	# `daemon.signing_pubkey_cache` on boot, alongside the seed + membership
	# keys, so the trust directory survives a restart.
	signing_pubkeys: dict[HostID, str] = field(default_factory=dict)
	# §13.3 duplicate-suppression cache — an insertion-ordered set used as a
	# bounded LRU. An `OrderedDict[key -> None]` gives O(1) membership,
	# O(1) insert, O(1) `move_to_end` on a hit, and O(1) `popitem(last=False)`
	# eviction at `seen_capacity` — no deque + `set(...)` rebuild on every apply
	# (which was O(n) over a 10k-entry cache, a per-record perf/DoS footgun). The
	# persisted wire shape stays a list of `[origin, kind, generation]` in order.
	seen: "OrderedDict[tuple[str, str, Generation], None]" = field(default_factory=OrderedDict)
	# This host's own per-origin Generation counter — loaded from disk on
	# restart, bumped + persisted on every membership- or ownership-affecting
	# change (spec §10.1, §12.1). Persisted so a crash-restart produces
	# `persisted+1`, never `1` (would let a stale low-gen record overwrite a
	# peer's newer view).
	own_generation: Generation = 0
	seen_capacity: int = 10_000

	# --- apply rules ---------------------------------------------------------

	def apply_membership(
		self,
		incoming: MembershipRecord,
		*,
		pubkey_cache: dict[str, str] | None = None,
	) -> bool:
		"""Apply the §13.2 rule for a Membership Record: replace iff the incoming
		Generation is strictly higher than the existing record's for this origin.
		Returns True iff the table changed. The dedupe key is recorded regardless
		(re-delivery of the same generation is a no-op apply but still gets
		cached, so we don't keep forwarding the byte-equal record).

		When `pubkey_cache` is provided and the incoming record carries a
		`signing_public_key`, the cache is updated so the envelope verifier
		(§19.1) can find the latest key for this origin. This allows key
		rotation via a higher-generation signed MembershipRecord."""
		existing = self.membership.get(incoming.host_id)
		changed = membership_replaces(existing, incoming)
		if changed:
			self.membership[incoming.host_id] = incoming
			# A late-refuting host (§14.5) whose membership we'd reaped into the
			# render-only `routable_dead` view is now live again — drop the stale
			# dead record so it isn't carried alongside the fresh one.
			self.routable_dead.pop(incoming.host_id, None)
			if pubkey_cache is not None and incoming.signing_public_key:
				pubkey_cache[incoming.host_id] = incoming.signing_public_key
		self._mark_seen(dedupe_key_membership(incoming))
		return changed

	def apply_ownership(self, incoming: OwnershipAdvertisement) -> bool:
		"""Apply the §13.2 rule for an Ownership Advertisement: same per-origin
		monotonic rule. Returns True iff the table changed."""
		existing = self.ownership.get(incoming.origin)
		changed = ownership_replaces(existing, incoming)
		if changed:
			self.ownership[incoming.origin] = incoming
		self._mark_seen(dedupe_key_ownership(incoming))
		return changed

	def seen_already(self, key: tuple[str, str, Generation]) -> bool:
		"""Check the §13.3 duplicate cache. The cache is an LRU bounded by
		`seen_capacity`; a hit short-circuits verify + apply + forward. A hit
		refreshes the key's LRU position (`move_to_end`) so a still-arriving
		duplicate isn't evicted out from under an ongoing partition heal. O(1)."""
		if key in self.seen:
			self.seen.move_to_end(key)
			return True
		return False

	def _mark_seen(self, key: tuple[str, str, Generation]) -> None:
		"""Record a dedupe key, evicting the oldest if at capacity (LRU). A
		duplicate re-mark just refreshes the key's position (`move_to_end`); a
		fresh key is appended and the oldest evicted once over `seen_capacity`.
		All O(1)."""
		if key in self.seen:
			self.seen.move_to_end(key)
			return
		self.seen[key] = None
		while len(self.seen) > self.seen_capacity:
			self.seen.popitem(last=False)

	def bump_own_generation(self) -> Generation:
		"""Increment the host's own per-origin Generation counter. Callers
		persist via `save_state()` after the change. Returns the new generation
		to advertise."""
		self.own_generation += 1
		return self.own_generation

	def gc_origin(self, origin: HostID) -> bool:
		"""Spec §14.6: drop the ownership advertisement for `origin` after the
		observer-local `ownership_grace` elapsed. Used by `FailureTracker.gc`
		via the loop's ownership reaping step — the ladder marks a host `dead`,
		then `dead_grace` elapses (membership reaped), then `ownership_grace`
		elapses (the routes the dead host advertised are reaped; if the /128s
		survived by another origin advertising them, they route to the
		successor — a normal ownership update; if no one advertises them, they
		simply vanish from the effective table). Returns True if the origin was
		removed. Also drops the origin's render-only `routable_dead` record (§14.3)
		— once its ownership is gone there is nothing left to route to it, so its
		`[Peer]` must disappear too."""
		self.routable_dead.pop(origin, None)
		return self.ownership.pop(origin, None) is not None

	def gc_origin_if_dead(
		self, origin: HostID, *, dead_at: float, ownership_grace: float, now: float
	) -> bool:
		"""Reap the origin's ownership advertisement iff `ownership_grace` has
		elapsed since the host was declared dead. The `dead_at` comes from
		`FailureTracker.dead_at`; the deadline check is `now - dead_at >=
		ownership_grace`. Returns True iff the advertisement was reaped this
		call. Per §14.3, the window is longer than `suspect_timeout + dead_grace`
		so a late-refuting host doesn't lose its routes mid-refute."""
		if origin not in self.ownership:
			return False
		if now - dead_at < ownership_grace:
			return False
		self.ownership.pop(origin, None)
		# The render-only `[Peer]` outlived membership only to keep the dead
		# host's /128s routable during the refute window; with its ownership now
		# reaped there is nothing left to route, so drop it too (§14.3).
		self.routable_dead.pop(origin, None)
		return True

	# --- serialization ------------------------------------------------------

	def to_dict(self) -> dict:
		return {
			"membership": {h: _membership_to_dict(m) for h, m in self.membership.items()},
			"ownership": {o: _ownership_to_dict(a) for o, a in self.ownership.items()},
			"routable_dead": {h: _membership_to_dict(m) for h, m in self.routable_dead.items()},
			"signing_pubkeys": dict(self.signing_pubkeys),
			# Persist the LRU keys in insertion order (oldest first) so a restart
			# reconstructs the same eviction order. Same wire shape as before
			# (a list of `[origin, kind, generation]`), so old files load as-is.
			"seen": [list(k) for k in self.seen],
			"own_generation": self.own_generation,
		}

	@classmethod
	def from_dict(cls, data: dict, *, seen_capacity: int = 10_000) -> "AppliedState":
		"""Reconstruct from the persisted JSON. Tolerates a missing `seen` /
		`own_generation` field (older files) by defaulting."""
		st = cls(seen_capacity=seen_capacity)
		for h, m in (data.get("membership") or {}).items():
			st.membership[h] = _membership_from_dict(m)
		for o, a in (data.get("ownership") or {}).items():
			st.ownership[o] = _ownership_from_dict(a)
		for h, m in (data.get("routable_dead") or {}).items():
			st.routable_dead[h] = _membership_from_dict(m)
		for h, pub in (data.get("signing_pubkeys") or {}).items():
			st.signing_pubkeys[h] = pub
		# `seen` is (and always was) a list of `[origin, kind, generation]` in
		# order; reconstruct the ordered set from it, tolerating the old list
		# format directly (the wire shape is unchanged).
		for k in data.get("seen") or []:
			st.seen[tuple(k)] = None
		st.own_generation = int(data.get("own_generation") or 0)
		return st


def load_state(data_dir: str, *, seen_capacity: int = 10_000) -> AppliedState:
	"""Load the persisted `AppliedState` from `<data_dir>/state.json`. A missing
	file returns an empty state (first boot, or a clean re-provision). A corrupt
	file raises — fail loud (Taste.md); the daemon should not paper over a
	broken persistence with empty state and silently re-advertise gen 1."""
	path = Path(data_dir) / _STATE_FILENAME
	if not path.exists():
		return AppliedState(seen_capacity=seen_capacity)
	with path.open("r", encoding="utf-8") as fh:
		return AppliedState.from_dict(json.load(fh), seen_capacity=seen_capacity)


def _fsync_dir(path: Path) -> None:
	"""fsync the directory `path` so a preceding `os.replace` into it is durable
	across a crash/power-cut. Without this the rename can be lost even though
	`os.replace` returned — the directory entry lives only in the page cache
	until the dir inode is flushed. Linux is the target (`O_DIRECTORY`); on a
	platform without it we degrade to a no-op rather than fail the write."""
	dir_flag = getattr(os, "O_DIRECTORY", 0)
	if not dir_flag:
		return
	fd = os.open(str(path), dir_flag)
	try:
		os.fsync(fd)
	finally:
		os.close(fd)


def save_state(state: AppliedState, data_dir: str) -> None:
	"""Atomically persist `state` to `<data_dir>/state.json` via a tempfile +
	`os.replace`, so a crash mid-write leaves the previous file intact (the
	same idiom the Task scripts use for env files). Creates the dir if missing.
	The parent dir is fsync'd after the rename so the replace is durable across
	a power-cut, not just the tempfile's contents.
	"""
	p = Path(data_dir)
	p.mkdir(parents=True, exist_ok=True)
	tmp = tempfile.NamedTemporaryFile("w", dir=p, delete=False, suffix=".tmp", encoding="utf-8")
	try:
		json.dump(state.to_dict(), tmp, indent=2, sort_keys=True)
		tmp.write("\n")
		tmp.flush()
		os.fsync(tmp.fileno())
		tmp.close()
		os.replace(tmp.name, p / _STATE_FILENAME)
		_fsync_dir(p)
	except Exception:
		tmp.close()
		try:
			os.unlink(tmp.name)
		except FileNotFoundError:
			pass
		raise


# --- record (de)serializers (kept here so records.py stays I/O-free) ---------


def _membership_to_dict(m: MembershipRecord) -> dict:
	d = {
		"host_id": m.host_id,
		"kind": m.kind.value,
		"state": m.state.value,
		"endpoint": m.endpoint,
		"wg_public_key": m.wg_public_key,
		"mesh_address": m.mesh_address,
		"generation": m.generation,
	}
	if m.signing_public_key:
		d["signing_public_key"] = m.signing_public_key
	return d


def _membership_from_dict(d: dict) -> MembershipRecord:
	return MembershipRecord(
		host_id=d["host_id"],
		kind=MembershipKind(d["kind"]),
		state=MemberState(d["state"]),
		endpoint=d["endpoint"],
		wg_public_key=d["wg_public_key"],
		mesh_address=d["mesh_address"],
		generation=int(d["generation"]),
		signing_public_key=d.get("signing_public_key", ""),
	)


def _ownership_to_dict(a: OwnershipAdvertisement) -> dict:
	d = {"origin": a.origin, "generation": a.generation, "owned": sorted(a.owned)}
	if a.signature:
		d["signature"] = a.signature
	return d


def _ownership_from_dict(d: dict) -> OwnershipAdvertisement:
	return OwnershipAdvertisement(
		origin=d["origin"],
		generation=int(d["generation"]),
		owned=frozenset(d["owned"]),
		signature=d.get("signature", ""),
	)


__all__ = ["AppliedState", "load_state", "save_state"]
