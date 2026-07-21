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
from collections import deque
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
	seen: deque[tuple[str, str, Generation]] = field(default_factory=deque)
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
		`seen_capacity`; a hit short-circuits apply + forward."""
		return key in set(self.seen)  # O(n) but n ≤ 10k and apply is not hot

	def _mark_seen(self, key: tuple[str, str, Generation]) -> None:
		"""Record a dedupe key, evicting the oldest if at capacity (LRU). A
		duplicate append is a no-op (the deque is treated as a set with order)."""
		if key in set(self.seen):
			return
		self.seen.append(key)
		while len(self.seen) > self.seen_capacity:
			self.seen.popleft()

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
		removed."""
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
		return True

	# --- serialization ------------------------------------------------------

	def to_dict(self) -> dict:
		return {
			"membership": {h: _membership_to_dict(m) for h, m in self.membership.items()},
			"ownership": {o: _ownership_to_dict(a) for o, a in self.ownership.items()},
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
		for k in data.get("seen") or []:
			st.seen.append(tuple(k))
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


def save_state(state: AppliedState, data_dir: str) -> None:
	"""Atomically persist `state` to `<data_dir>/state.json` via a tempfile +
	`os.replace`, so a crash mid-write leaves the previous file intact (the
	same idiom the Task scripts use for env files). Creates the dir if missing.
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
