"""Record types, generation semantics, and the effective-table computation
(spec §7). Pure: dataclasses + small functions, no I/O, no host touch.

Two record kinds exist:

- `MembershipRecord` (spec §7.1) — one per compute host, origin == host_id,
  mutated by the origin only (the §19 cross-origin forwarding ban). `kind` and
  `state` are the *origin's view* (alive/leaving / alive-suspect-dead as the
  origin asserts); the observer-local suspicion ladder (spec §14.1) is a
  separate field the observer keeps in `state.py`'s persisted view, never on the
  wire.

- `OwnershipAdvertisement` (spec §7.2) — a per-origin FULL SET of the /128s the
  origin currently owns. origin == owner_host always. Never a delta; removing a
  /128 is a later advertisement with a smaller set at a higher generation. No
  cross-origin generation comparison ever happens (Issue C close-out): the
  effective table is the union of the latest advertisement per origin, and a
  /128 in two origins' sets is the §7.3 conflict.

Generations are 64-bit unsigned, monotonic per-origin, persisted to disk so a
crash-restart does not reset them (spec §7.1, §13.4). We model them as a plain
`int` — the monotonicity invariant is enforced at apply time (spec §13.2), not
by the type.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import Enum

HostID = str
IP6 = str
Generation = int


class MembershipKind(str, Enum):
	"""The origin's declared intent: a normal member, or shutting down (§14.4)."""

	MEMBER = "member"
	LEAVING = "leaving"


class MemberState(str, Enum):
	"""The origin's asserted state on the wire (§14.1 quirk). The observer-local
	suspicion ladder (alive→suspect→dead) is recomputed locally; the wire state
	is the origin's own claim — normally `alive`, never `suspect` (only the
	observer suspects), `dead` not carried (dead records are GC'd, §14.6)."""

	ALIVE = "alive"
	LEAVING = "leaving"


@dataclass(frozen=True, slots=True)
class MembershipRecord:
	"""One compute host, as gossiped (spec §7.1). Origin == `host_id`."""

	host_id: HostID
	kind: MembershipKind
	state: MemberState
	endpoint: str  # bare public IPv6 (no port); render wraps [{endpoint}]:{port}
	wg_public_key: str  # base64 STANDARD (32 raw Curve25519 bytes)
	mesh_address: IP6  # fdaa:0:0:<idx>::1 — infra /48 bus address
	generation: Generation
	# §19.3 signing pubkey (base64 ed25519, 32 raw bytes). Rides the record so a
	# verifier can look up the right key for the signature. Absent for a
	# record that pre-dates Stage 5 (a stub default "" — a verifier treats
	# the empty string as "no signature required", the development/test path
	# before signing is wired). Stage 5 always sets it from `keys.ensure_signing_keypair`.
	signing_public_key: str = ""

	def origin(self) -> HostID:
		"""The origin of this record — always `host_id` (§19.2)."""
		return self.host_id


@dataclass(frozen=True, slots=True)
class OwnershipAdvertisement:
	"""A per-origin FULL SET of owned /128s at a given Generation (spec §7.2).

	`origin == owner_host` always (§19.2); a relay forwards this record but only
	the origin may publish it. The set is a frozenset so equality is order-
	insensitive — two advertisements with the same set + generation are the same
	advertisement, which the duplicate-suppression cache (spec §13.3) relies on.
	"""

	origin: HostID
	generation: Generation
	owned: frozenset[IP6]
	# Carried but NOT part of the equality/dedupe key (the signature is part of
	# the wire bytes but the record's identity is (origin, generation) only —
	# see records._record_key in gossip). Default "" — unsigned test path.
	signature: str = ""


@dataclass(frozen=True, slots=True)
class OwnershipTable:
	"""The effective ownership table (spec §7.2), derived — never stored.

	- `owner_of[ip]` is the unique HostID whose latest advertisement claims
	  `ip`. Populated ONLY for /128s in zero origins (impossible) or one origin;
	  a /128 in two or more origins is in `conflicts` and NOT in `owner_of`
	  (spec §7.3 — drop + report, never elect).
	- `conflicts` is the set of /128s claimed by ≥ 2 origins. Routing for them
	  is dropped at §16.3 until the virtualisation layer resolves the
	  double-ownership.
	"""

	owner_of: dict[IP6, HostID] = field(default_factory=dict)
	conflicts: frozenset[IP6] = field(default_factory=frozenset)


def effective_ownership(latest_per_origin: dict[HostID, OwnershipAdvertisement]) -> OwnershipTable:
	"""Compute the effective Ownership table as the union of the latest
	advertisement per origin (spec §7.2). A /128 in two+ origins' active sets is
	a conflict (§7.3): it lives in `conflicts`, NOT in `owner_of`. Generations
	are NOT compared across origins (Issue C); they only compete within an
	origin, which the caller's apply rule (§13.2) already enforced before
	storing into `latest_per_origin`.
	"""
	hits: dict[IP6, list[HostID]] = {}
	for origin, adv in latest_per_origin.items():
		for ip in adv.owned:
			hits.setdefault(ip, []).append(origin)
	owner_of: dict[IP6, HostID] = {}
	conflicts: set[IP6] = set()
	for ip, origins in hits.items():
		if len(origins) == 1:
			owner_of[ip] = origins[0]
		else:
			# ≥ 2 distinct origins claim this /128 — conflict, never elect.
			# (Defensive: collapse duplicates in case an origin appears twice
			# in the input — it shouldn't, but the rule must hold regardless.)
			if len(set(origins)) > 1:
				conflicts.add(ip)
			else:
				owner_of[ip] = origins[0]
	return OwnershipTable(owner_of=owner_of, conflicts=frozenset(conflicts))


def membership_replaces(existing: MembershipRecord | None, incoming: MembershipRecord) -> bool:
	"""The §10.3 / §13.2 apply rule for a Membership Record: an incoming record
	replaces the existing one iff its Generation is strictly higher (same origin
	by §19.2; cross-origin forwarding is rejected upstream). Equal-generation is
	a no-op (idempotent re-delivery); lower-generation is a stale replay to drop.
	"""
	return existing is None or incoming.generation > existing.generation


def ownership_replaces(existing: OwnershipAdvertisement | None, incoming: OwnershipAdvertisement) -> bool:
	"""The §13.2 apply rule for an Ownership Advertisement: same per-origin
	monotonic rule as `membership_replaces`. The full-set model means an equal-
	generation re-delivery is byte-equal (frozenset), so dropping it on equality
	is also correct; we use strict `>` to match the membership rule and let
	the duplicate-suppression cache (§13.3) catch byte-equal redelivery.
	"""
	return existing is None or incoming.generation > existing.generation


def dedupe_key_membership(record: MembershipRecord) -> tuple[str, str, Generation]:
	"""(origin, kind, generation) — the §13.3 duplicate-suppression key."""
	return (record.host_id, "membership", record.generation)


def dedupe_key_ownership(record: OwnershipAdvertisement) -> tuple[str, str, Generation]:
	"""(origin, kind, generation) — the §13.3 duplicate-suppression key."""
	return (record.origin, "ownership", record.generation)


def owning_advertisement(
	origin: HostID, generation: Generation, owned: Iterable[IP6]
) -> OwnershipAdvertisement:
	"""Build an advertisement; frozenset coercion keeps equality order-
	insensitive (the §13.3 cache + the §16.2 render both depend on this)."""
	return OwnershipAdvertisement(origin=origin, generation=generation, owned=frozenset(owned))


__all__ = [
	"IP6",
	"Generation",
	"HostID",
	"MemberState",
	"MembershipKind",
	"MembershipRecord",
	"OwnershipAdvertisement",
	"OwnershipTable",
	"dedupe_key_membership",
	"dedupe_key_ownership",
	"effective_ownership",
	"membership_replaces",
	"ownership_replaces",
	"owning_advertisement",
	"replace",
]
