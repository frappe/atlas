"""Peer selection for gossip fan-out (spec §13.1).

Stage 2 ships the simple random selection the original SWIM paper uses
uniformly across the membership table, excluding self. Stage 4 (SWIM with
Lifeguard) layers health-aware weighting on top — that lives in a separate
module so this one stays a tiny, testable selector and the swap is local.

The selector is a pure function over the membership dict + a randomness source;
the caller injects the randomness (tests pass a seeded `random.Random`) so a
	test run is reproducible without the host.
"""

from __future__ import annotations

import random
from collections.abc import Callable

from .records import HostID, MembershipRecord

# Type alias — the injectable randomness the gossip handler passes in. A test
# seeds this for deterministic fan-out; the daemon wires the module-level
# `random.Random()` instance.
RandomFn = Callable[[list[HostID]], list[HostID]]


def make_random_selector(bound_random: random.Random) -> RandomFn:
	"""Return a `RandomFn` that picks `count` distinct members uniformly at
	random from `members` (excluding `self_host_id`), using the injected
	randomness source. The caller sets the count via the closure returned here
	so the selector is one callable; we curried the count because callers want
	one `random_fn(members_excluding_self)` API and we don't want the gossip
	round to know how many peers to ask — that's a Config concern (`gossip_fanout`).
	"""

	# We close over `bound_random` so a test's seeded RNG keeps fan-out
	# reproducible; the daemon uses the module-level `random` for entropy.
	def _selector(pool: list[HostID]) -> list[HostID]:
		return bound_random.sample(pool, k=0) if not pool else bound_random.sample(pool, k=min(len(pool), 1))

	return _selector


def select_peers(
	members: dict[HostID, MembershipRecord],
	self_host_id: HostID,
	count: int,
	*,
	rng: random.Random | None = None,
) -> list[HostID]:
	"""The gossip fan-out pick (spec §13.1). Uniformly-random `count` distinct
	members, excluding self. Returns an empty list rather than crashing if the
	(excluding-self) membership has 0 entries — the daemon comes up peer-empty
	awaiting its first anti-entropy fill (§9.1), and a lone host simply doesn't
	gossip this round.

	`rng` is a `random.Random` instance the caller injects; None uses a fresh
	module-level RNG (the daemon's path). A test passes a seeded Random so the
	sequence of picks is reproducible — the gossip round's FSM is unit-testable
	without the kernel.
	"""
	# Stage 4 will replace `rng` with a health-aware sampler (recently-probed /
	# flapping peers downweighted, Lifeguard-style). The function signature
	# stays exactly this shape — health-aware is a drop-in swap here.
	pool = [h for h in members if h != self_host_id and members[h].state.value == "alive"]
	if not pool or count <= 0:
		return []
	(rng or random).shuffle(pool)
	return pool[: min(count, len(pool))]


__all__ = ["RandomFn", "make_random_selector", "select_peers"]
