"""Observer-local failure tracking (spec §14.1 / §14.3 — Issue D close-out).

The wire `MembershipRecord.state` is the **origin's view** (`alive` or
`leaving`); never `suspect` (only an observer suspects). The observer-local
ladder `alive → suspect → dead` lives here, persistable alongside the wire
records but never sent over the wire.

The ladder is driven by `ProbeProtocol` (the SWIM ping cycle, `probe.py`):

  - alive: probes healthy; the host is routable.
  - suspect: a direct + indirect probe failed. Refute window opens for
    `suspect_timeout` (the operator-tunable partition knob); the host may
    fast-refute by emitting an `alive` Membership Record at a fresh Generation,
    which clears the suspicion (the §10.3 monotonic apply rule already accepts
    the refute — Stage 4 wires the trigger).
  - dead: `suspect_timeout` elapsed with no refute. After `dead_grace`, the
    record is GC'd (the host is removed from the membership table; gossip +
    anti-entropy stop targeting it; its ownership advertisements stay until
    `ownership_grace` to give the host a window to refute late).

Spec §14.3: `ownership_grace > suspect_timeout + dead_grace` — a host that
refutes late (partition just long enough to hit suspect, then recovers within
`ownership_grace`) does not lose its routes mid-refute.

`FailureTracker` keeps the per-peer ladder state in one persisted structure so
§14.5 crash recovery (a daemon restart mid-suspicion) doesn't reset the
observer's suspicion clocks. The persisted shape is:

    {
      "h2": {"state": "suspect", "since": 1234567.89, "last_probed": 1234123.4},
      ...
    }
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from .records import HostID


class FailureState(str, Enum):
	"""Observer-local. `suspect` and `dead` are NOT the wire record's `state`
	field — they're the observer's private view of another host. The wire
	`MemberState` only carries `alive` or `leaving` (the origin's view)."""

	ALIVE = "alive"
	SUSPECT = "suspect"
	DEAD = "dead"


@dataclass(slots=True)
class PeerFailureState:
	"""One observer's view of one peer. `since` is the wall clock (via the
	injected `now_fn`) at which the peer entered its current state — drives
	the `suspect_timeout` / `dead_grace` deadlines. `last_probed` is the wall
	clock at which we last sent a direct or indirect probe to this peer; used
	by the health-aware sampler to underweight recently-probed peers."""

	state: FailureState = FailureState.ALIVE
	since: float = 0.0
	last_probed: float = 0.0


@dataclass(slots=True)
class FailureTracker:
	"""The observer-local ladder + garbage collection of dead peers' records.
	Sizes: `peers` is O(N) — one `PeerFailureState` per alive/suspect/dead
	peer; `dead_at` is O(#dead) — a timestamp per host that's been declared
	dead but not yet GC'd (`dead_grace`); `leaving_at` is O(#leaving) — a
	timestamp per host that announced graceful shutdown (§14.4) and is being
	fast-pathed alive → dead after `leaving_grace`."""

	peers: dict[HostID, PeerFailureState] = field(default_factory=dict)
	dead_at: dict[HostID, float] = field(default_factory=dict)
	# §14.3/§14.4 — a host that advertised `kind=leaving` records its leave
	# timestamp here; the loop's GC tick (`promote_leaving_if_due`) marks it
	# `dead` DIRECTLY (skipping `suspect`) once `leaving_grace` elapses. A
	# fast-restart that re-advertises `alive` before the grace elapses clears
	# this via `note_alive` (§14.4 step 3 refute).
	leaving_at: dict[HostID, float] = field(default_factory=dict)
	# The injected clock — `time.monotonic` in production, controlled in tests.
	now_fn: Callable[[], float] = field(default=lambda: 0.0)

	# --- query ----------------------------------------------------------------

	def state_of(self, host_id: HostID) -> FailureState:
		"""Read the observer's view of one peer. Alive if we've never heard of
		them (the default — a new peer starts alive; §10 suspicion only fires
		when a probe actually fails)."""
		peer = self.peers.get(host_id)
		return peer.state if peer is not None else FailureState.ALIVE

	def last_probed(self, host_id: HostID) -> float:
		peer = self.peers.get(host_id)
		return peer.last_probed if peer is not None else 0.0

	# --- transitions ---------------------------------------------------------

	def note_probed(self, host_id: HostID) -> None:
		"""Record that we just sent a probe to `host_id` (direct or indirect).
		Does NOT change the ladder state; just updates `last_probed` so the
		health-aware sampler can underweight this peer for the next interval."""
		peer = self.peers.setdefault(host_id, PeerFailureState())
		peer.last_probed = self.now_fn()

	def note_alive(self, host_id: HostID) -> None:
		"""Fast-refute trigger (§14.2 step 5 paragraph / §14.5): a host we had
		marked `suspect` (or even `dead` within `dead_grace`) cleared the
		suspicion by sending us an `alive` Membership Record at a higher
		Generation than we had stored. Reset its observer-local state to
		`alive` and drop any `dead_at` GC timer we'd armed.

		Also cancels a pending leaving → dead countdown (§14.4 step 3): a
		sub-`leaving_grace` `systemctl restart` re-advertises `alive` at a
		higher Generation before the grace elapses; that refute keeps the host
		alive instead of reaping it."""
		peer = self.peers.setdefault(host_id, PeerFailureState())
		peer.state = FailureState.ALIVE
		peer.since = self.now_fn()
		self.dead_at.pop(host_id, None)
		self.leaving_at.pop(host_id, None)

	def note_leaving(self, host_id: HostID) -> None:
		"""Graceful-shutdown notice (§14.4): the origin advertised a
		`kind=leaving` Membership Record. Arm the leaving → dead countdown from
		`now`; the loop's `promote_leaving_if_due` marks the host `dead`
		DIRECTLY (skipping `suspect`) once `leaving_grace` elapses. We do NOT
		touch the observer-local ladder state here — the host stays wherever it
		was on the wire (its record carries `state=leaving`, which already
		excludes it from probe/gossip/anti-entropy target selection); the
		countdown is the sole driver toward `dead`.

		Idempotent: a re-delivery of the same leaving record (or a later one at
		a higher generation) keeps the ORIGINAL `leaving_at` so the grace runs
		from the first notice, not the last."""
		self.leaving_at.setdefault(host_id, self.now_fn())

	def promote_leaving_if_due(self, leaving_grace: float) -> list[HostID]:
		"""§14.4: mark `dead` any host whose `leaving_at` is older than
		`leaving_grace`, skipping `suspect`. Clears the `leaving_at` entry (the
		host is now on the normal `dead_grace`/`ownership_grace` ladder driven
		by `mark_dead`'s `dead_at`). Returns the host_ids promoted this tick."""
		now = self.now_fn()
		promoted: list[HostID] = []
		for host_id in list(self.leaving_at.keys()):
			if now - self.leaving_at[host_id] < leaving_grace:
				continue
			self.leaving_at.pop(host_id, None)
			self.mark_dead(host_id)
			promoted.append(host_id)
		return promoted

	def mark_suspect(self, host_id: HostID) -> None:
		"""A direct + indirect probe failed (§14.2 step 5). Move the peer from
		`alive` to `suspect`; armed the `suspect_timeout` from `now`. If the
		peer was already `suspect`, this is a no-op (we don't double-mark). If
		the peer was `dead`, the suspicion is moot — a dead host never
		re-enters suspicion; it stays dead until it refutes (which resets via
		`note_alive`)."""
		peer = self.peers.setdefault(host_id, PeerFailureState())
		if peer.state == FailureState.DEAD:
			return  # dead hosts don't re-enter the ladder through probe failure
		peer.state = FailureState.SUSPECT
		peer.since = self.now_fn()

	def mark_dead(self, host_id: HostID) -> None:
		"""`suspect_timeout` elapsed with no refute (§14.3 / §14.6). Move to
		`dead` and arm `dead_grace` for GC. Dead hosts are still kept in the
		membership table until `dead_grace` elapses (so gossip + anti-entropy
		know their last-known state to inform other peers that they're gone);
		after GC they're removed entirely (`gossip` and `anti-entropy` no
		longer target them, and a returning host rejoins via the normal
		§9.1 cold-join path)."""
		peer = self.peers.setdefault(host_id, PeerFailureState())
		if peer.state == FailureState.DEAD:
			return  # idempotent — doesn't reset `dead_at`
		peer.state = FailureState.DEAD
		peer.since = self.now_fn()
		self.dead_at[host_id] = self.now_fn()

	def gc(self, suspect_timeout: float, dead_grace: float, ownership_grace: float, state) -> list[HostID]:
		"""Run one GC tick (called from the loop every probe round):
		  1. Promote suspect→dead for any peer past `suspect_timeout` (the
		     missing ladder step — §14.3).
		  2. Reap any `dead` peer whose `dead_grace` has elapsed from `dead_at`.

		Returns the list of host_ids whose MEMBERSHIP was reaped this round
		(the loop uses this to schedule a wg-mesh re-render). Ownership
		records are NOT reaped here — they outlast membership by
		`ownership_grace` (§14.3), so we keep the `dead_at` entry until the
		loop's ownership-reap step (`gc_origin_if_dead`) clears it past
		`ownership_grace`. Without this, a reaped host's ownership records
		leak forever — the loop's step 2 iterates `dead_at` and can't find
		the timestamp once we popped it."""
		now = self.now_fn()
		# 1) Promote suspect → dead (§14.3).
		for host_id in list(self.peers.keys()):
			peer = self.peers[host_id]
			if peer.state == FailureState.SUSPECT and now - peer.since >= suspect_timeout:
				self.mark_dead(host_id)
		# 2) Reap membership past `dead_grace`. Keep `dead_at` + the `peers`
		# ladder entry alive until `ownership_grace` elapses — the loop's
		# ownership-reap step (`gc_origin_if_dead`) needs the timestamp and
		# pops `dead_at` itself once it has reaped the ownership records.
		#
		# §14.3: a host reaped from `membership` here that still has live
		# Ownership Records is moved to `state.routable_dead` — a render-ONLY
		# view — so its `[Peer]` keeps carrying its /128s until its ownership is
		# also reaped at `ownership_grace`. It is gone from `membership`, so
		# every peer-selection / probe / anti-entropy path (all of which read
		# `membership`) stops targeting it — it is not probed or gossiped-to,
		# only rendered. A host that owns NOTHING is reaped outright (no
		# `routable_dead` entry) — we don't leak membership for a host with no
		# routes to keep alive.
		reaped: list[HostID] = []
		for host_id in list(self.dead_at.keys()):
			if now - self.dead_at[host_id] < dead_grace:
				continue
			record = state.membership.pop(host_id, None)
			if record is not None and host_id in state.ownership:
				state.routable_dead[host_id] = record
			reaped.append(host_id)
		return reaped


__all__ = ["FailureState", "FailureTracker", "PeerFailureState"]
