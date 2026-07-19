"""The main loop (spec §11.2 + §16.4 + §14.4). Pure scheduling on top of the
`Daemon`'s single-responsibility methods — kept tiny (< 10 lines per function)
so the timing invariants are visible at a glance.

    every tick (config.gossip_interval, default 200 ms):
        if scan due   → daemon.scan_local_ownership(); if changed → schedule apply
        if apply due (debounced by config.apply_debounce, default 200 ms) → apply
        pat the systemd watchdog (§: WATCHDOG=1, so `WatchdogSec=` relaunches a stuck daemon)

The loop has no host touch — every side-effect goes through `Daemon`'s injected
seams. `main.py` wires the signal handlers + sd_notify around this.

Stage 1b: only the scan + apply half of the loop is wired (no peer traffic yet).
The same loop, with the gossip/anti-entropy/probe handlers plugged in, is what
Stage 2 / 3 / 4 extend — this scheduling core is shared and unchanged.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .antientropy import anti_entropy_round
from .daemon import Daemon
from .gossip import GossipState, gossip_round, handle_message
from .probe import ProbeProtocol


@dataclass(slots=True)
class ScheduledApply:
	"""The debounce timer (spec §16.4 — `apply_debounce`, default 200 ms). When
	a change arrives (a scan detected a local-ownership difference), the loop
	sets `due_at = now + apply_debounce`; subsequent changes within the window
	are absorbed into the same apply. This is what makes a /128 that hops twice
	in quick succession produce ONE syncconf, not two (which would risk the
	§16.3 invariant at the in-between state)."""

	due_at: float | None = None

	def schedule(self, now: float, debounce: float) -> None:
		"""If no apply is pending, schedule one at `now + debounce`. If one is
		already pending, leave its deadline (a burst collapses into one apply,
		debounced from the FIRST change, not the last — bounded latency)."""
		if self.due_at is None:
			self.due_at = now + debounce

	def consume_if_due(self, now: float) -> bool:
		"""True iff an apply is pending AND `now ≥ its deadline`. Clears the
		timer so the apply runs exactly once per burst."""
		if self.due_at is None or now < self.due_at:
			return False
		self.due_at = None
		return True


@dataclass(slots=True)
class Loop:
	"""The scheduling core. `tick_interval` defaults to `config.gossip_interval`
	(200 ms); `now_fn` is `time.monotonic` in production and injected in tests.
	`running` is flipped to False by the SIGTERM handler in `main.py`."""

	daemon: Daemon
	tick_interval: float
	now_fn: Callable[[], float] = field(default=time.monotonic)
	running: bool = True
	apply: ScheduledApply = field(default_factory=ScheduledApply)
	gossip_state: GossipState = field(default_factory=GossipState)
	# The probe protocol + the failure tracker — Stage 4. Optional: a test
	# loop can leave both None to exercise only the gossip scan/apply/gossip
	# path; `main.py` always wires them in production.
	probe_protocol: ProbeProtocol | None = field(default=None)
	_next_scan_at: float = 0.0
	_next_gossip_at: float = 0.0
	_next_anti_entropy_at: float = 0.0
	_next_probe_at: float = 0.0

	def run(self) -> None:
		"""Main loop body. Returns on `self.running = False` (SIGTERM). Each
		iteration covers at most: scan, apply, gossip fan-out, anti-entropy
		pull, probe round, GC, drain of incoming UDP, watchdog pat. Order is
		scoped so a slow step (large local-ownership scan, large anti-entropy
		response) doesn't starve the others."""
		now_start = self.now_fn()
		self._next_scan_at = now_start
		self._next_gossip_at = now_start
		self._next_anti_entropy_at = now_start
		self._next_probe_at = now_start
		while self.running:
			now = self._now()
			self._scan_if_due(now)
			self._apply_if_due(now)
			self._gossip_if_due(now)
			self._anti_entropy_if_due(now)
			self._probe_if_due(now)
			self._drain_incoming()
			self._check_probe_timeouts()
			self._gc_if_due(now)
			self.daemon.notify_watchdog()
			time.sleep(self.tick_interval)

	def _now(self) -> float:
		return self.now_fn()

	def _scan_if_due(self, now: float) -> None:
		"""Spec §11.2 — every `ownership_scan_interval` (default 2 s)."""
		if now < self._next_scan_at:
			return
		self._next_scan_at = now + self.daemon.config.ownership_scan_interval
		if self.daemon.scan_local_ownership():
			self.apply.schedule(now, self.daemon.config.apply_debounce)
			# A change in local ownership is news worth piggybacking on the
			# next gossip round — add our updated advertisement to the queue.
			self.gossip_state.note_applied(self.daemon.state.ownership[self.daemon.identity.host_id])

	def _apply_if_due(self, now: float) -> None:
		"""Spec §16.4 — runs atomic apply on the debounce deadline."""
		if self.apply.consume_if_due(now):
			self.daemon.apply_if_changed()

	def _gossip_if_due(self, now: float) -> None:
		"""Spec §13.1 — every `gossip_interval` (= the tick_interval here) we
		fan a Gossip out to `gossip_fanout` random peers. No-op when there's no
		transport bound yet (the cold-join path of §9.2 runs before the daemon
		has a socket) or no peers (lone host waiting for its first seed)."""
		if now < self._next_gossip_at:
			return
		self._next_gossip_at = now + self.daemon.config.gossip_interval
		t = self.daemon.transport
		if t is None:
			return
		gossip_round(self.daemon, t, self.gossip_state)

	def _anti_entropy_if_due(self, now: float) -> None:
		"""Spec §15.1 — every `anti_entropy_interval` (default 1 s) pick ONE
		random peer and pull its records we're missing. The anti-entropy reply
		arrives asynchronously on the next tick's _drain_incoming and is
		dispatched through `handle_message` → `handle_anti_entropy_resp`. This
		is the correctness backstop that doesn't depend on gossip's
		probabilistic delivery (§15.4 Demers' result)."""
		if now < self._next_anti_entropy_at:
			return
		self._next_anti_entropy_at = now + self.daemon.config.anti_entropy_interval
		t = self.daemon.transport
		if t is None:
			return
		anti_entropy_round(self.daemon, t)

	def _probe_if_due(self, now: float) -> None:
		"""Spec §14.2 — every `probe_interval` (default 1 s) pick
		`probe_peers` random alive members and ping each. Acks come back async
		on _drain_incoming; _check_probe_timeouts (called after the drain)
		marks the misses suspect after direct + indirect timeouts."""
		if now < self._next_probe_at:
			return
		self._next_probe_at = now + self.daemon.config.probe_interval
		t = self.daemon.transport
		if t is None or self.probe_protocol is None:
			return
		self.probe_protocol.probe_round(self.daemon, t)

	def _check_probe_timeouts(self) -> None:
		"""Reconcile in-flight pings against the clock after each drain. Each
		miss past `probe_timeout` gets K indirect relays; each further miss
		after `indirect_timeout` is marked suspect (§14.2)."""
		if self.probe_protocol is None or self.daemon.transport is None:
			return
		self.probe_protocol.check_timeouts(self.daemon, self.daemon.transport)

	def _gc_if_due(self, now: float) -> None:
		"""Spec §14.6 — run the FailureTracker's GC tick: reap memberships
		past `dead_grace` and ownership advertisements past `ownership_grace`
		for any origin we've marked dead. Cheap enough to run every loop tick
		(it's O(dead) — bounded by the number of dead hosts), so no separate
		cadence timer — we let it ride on the main loop and the tracker's
		deadlines keep the work down."""
		tracker = getattr(self.daemon, "failure_tracker", None)
		if tracker is None:
			return
		# 1) Reap membership records past `dead_grace`. `tracker.gc` keeps the
		# `dead_at` entry alive so step 2 can reap ownership past
		# `ownership_grace` (§14.3 — routes outlast the membership reaped
		# window). Without keeping `dead_at`, the ownership records leak
		# forever once the host is popped.
		reaped = tracker.gc(
			self.daemon.config.suspect_timeout,
			self.daemon.config.dead_grace,
			self.daemon.config.ownership_grace,
			self.daemon.state,
		)
		# 2) For each dead host still in `dead_at`, reap ownership past
		# `ownership_grace` AND clear the `dead_at` + ladder entry once the
		# ownership is reaped (the host is fully gone then).
		for host_id in list(tracker.dead_at.keys()):
			dead_at = tracker.dead_at[host_id]
			ownership_reaped = self.daemon.state.gc_origin_if_dead(
				host_id,
				dead_at=dead_at,
				ownership_grace=self.daemon.config.ownership_grace,
				now=now,
			)
			# Once BOTH the membership (reaped in step 1) AND the ownership
			# (reaped here, or no ownership record existed) are gone, clear
			# the `dead_at` entry + the ladder slot so the host is fully GC'd.
			if ownership_reaped or host_id in reaped:
				if ownership_reaped:
					self.apply.schedule(now, self.daemon.config.apply_debounce)
				# Pop `dead_at` only once `ownership_grace` has elapsed so a
				# late refuter (§14.3) doesn't lose its routes mid-window.
				if now - dead_at >= self.daemon.config.ownership_grace:
					tracker.dead_at.pop(host_id, None)
					tracker.peers.pop(host_id, None)
		if reaped:
			# A membership was reaped — schedule the apply so the peer
			# disappears from WgDesired's peer set.
			self.apply.schedule(now, self.daemon.config.apply_debounce)

	def _drain_incoming(self) -> None:
		"""Spec §13.2 — recv every pending UDP datagram (non-blocking) and
		dispatch through the same `handle_message` that join replies use. A
		freshly-applied record marks the apply as needing a re-render (the
		renderer reads the effective table from `state` next apply round)."""
		t = self.daemon.transport
		if t is None:
			return

		def _on_msg(msg, sender_addr) -> None:
			handle_message(msg, sender_addr, self.daemon, self.gossip_state)
			# A membership change (someone joined) or ownership change (a peer
			# advertised new /128s) potentially changes our desired wg-mesh
			# config. Schedule a debounced apply so we re-render + syncconf.
			self.apply.schedule(self._now(), self.daemon.config.apply_debounce)

		t.drain(_on_msg)


__all__ = ["Loop", "ScheduledApply"]
