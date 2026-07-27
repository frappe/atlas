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

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

from .antientropy import anti_entropy_round
from .daemon import Daemon
from .gossip import GossipState, gossip_round, handle_message
from .probe import ProbeProtocol
from .ratelimit import SourceRateLimiter
from .state import save_state


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
	# §9.2 — the cold-join retry. `main.py` seeds `cold_join_seeds` with the
	# same seeds passed to the one-shot `cold_join` call; the loop re-sends
	# the MembershipAdvertisement every `cold_join_retry_interval` (2 s)
	# until a peer datagram reaches `handle_message` (`_cold_join_done` flips
	# True in `_drain_incoming`), or until `cold_join_max_attempts` (15) is
	# hit. Closes the §9.2 one-shot failure mode: if the initial cold-join UDP
	# datagram drops, a newcomer would otherwise sit peer-empty forever —
	# gossip doesn't carry the `introduction_signature`, so the existing
	# hosts' verifier would reject every subsequent TYPE_GOSSIP from the
	# newcomer ("no introduction_signature"). Retried `cold_join` is the
	# only path that re-sends the introduction cert.
	cold_join_seeds: list | None = field(default=None)
	# §19 flood defense — the per-source rate limiter (keyed by remote IP,
	# checked BEFORE the ed25519 verify). Lazily built from `daemon.config` on
	# first drain so a test that constructs a bare Loop needn't wire it.
	_rate_limiter: SourceRateLimiter | None = field(default=None)
	_next_cold_join_at: float = 0.0
	_cold_join_attempts: int = 0
	_cold_join_done: bool = False
	_next_scan_at: float = 0.0
	_next_gossip_at: float = 0.0
	_next_anti_entropy_at: float = 0.0
	_next_probe_at: float = 0.0

	# §9.2 — the cold-join retry cadence. 2 s between attempts, 15 attempts
	# max → a newcomer keeps retrying for ~30 s, plenty for a UDP datagram to
	# land on at least one seed even over a flaky path.
	COLD_JOIN_RETRY_INTERVAL: ClassVar[float] = 2.0
	COLD_JOIN_MAX_ATTEMPTS: ClassVar[int] = 15

	def run(self) -> None:
		"""Main loop body. Returns on `self.running = False` (SIGTERM). Each
		iteration covers at most: scan, apply, gossip fan-out, anti-entropy
		pull, probe round, GC, drain of incoming UDP, watchdog pat. Order is
		scoped so a slow step (large local-ownership scan, large anti-entropy
		response) doesn't starve the others.

		M8 — every subsystem step runs under `_guard`: an `Exception` from any
		one step (a render/apply that throws, a bad handler) is logged +
		counted (`loop_step_error`) and the loop CONTINUES to the next step /
		tick rather than escaping `run()`. Without this, `Restart=on-failure`
		hot-loops the daemon every ~2 s and the wg-mesh peer table freezes (no
		applies) — the host stops converging, and a persisted trigger that
		re-crashes on reload is a permanent wedge. `KeyboardInterrupt` /
		`SystemExit` are NOT caught (they aren't `Exception` subclasses) so the
		SIGTERM/graceful-shutdown path still tears the loop down."""
		now_start = self.now_fn()
		self._next_scan_at = now_start
		self._next_gossip_at = now_start
		self._next_anti_entropy_at = now_start
		self._next_probe_at = now_start
		self._next_cold_join_at = now_start + self.COLD_JOIN_RETRY_INTERVAL
		while self.running:
			now = self._now()
			self._guard("scan", self._scan_if_due, now)
			self._guard("apply", self._apply_if_due, now)
			self._guard("gossip", self._gossip_if_due, now)
			self._guard("anti_entropy", self._anti_entropy_if_due, now)
			self._guard("probe", self._probe_if_due, now)
			self._guard("drain", self._drain_incoming)
			self._guard("cold_join", self._cold_join_if_due, now)
			self._guard("check_timeouts", self._check_probe_timeouts)
			self._guard("gc", self._gc_if_due, now)
			self._guard("watchdog", self.daemon.notify_watchdog)
			time.sleep(self.tick_interval)

	def _guard(self, step: str, fn: Callable, *args) -> None:
		"""Run one loop step, converting any `Exception` into a log + a
		`loop_step_error` counter bump so a single bad step degrades (skipped
		this tick) instead of killing the daemon (M8). Re-raises
		`KeyboardInterrupt` / `SystemExit` untouched — those drive graceful
		shutdown and MUST propagate. Catching bare `Exception` already excludes
		both (neither subclasses `Exception`), but the `try` body is minimal so
		nothing in the guard itself can mask a shutdown signal."""
		try:
			fn(*args)
		except Exception as exc:  # noqa: BLE001 — deliberate broad catch; see docstring
			counter = getattr(self.daemon, "metrics", None)
			if counter is not None:
				counter.incr("loop_step_error")
			print(
				f"atlas-networkd: ERROR: loop step {step!r} raised {type(exc).__name__}: {exc} "
				"— skipping this tick, loop continues (M8)",
				file=sys.stderr,
			)

	def _cold_join_if_due(self, now: float) -> None:
		"""§9.2 — re-send the MembershipAdvertisement to every seed until one
		replies (the reply arrives as a TYPE_GOSSIP bundle that drains through
		`_drain_incoming`; any datagram that reaches `handle_message` flips
		`_cold_join_done`), or until `COLD_JOIN_MAX_ATTEMPTS` is hit.

		Idempotent: a re-send at the same generation re-applies the same
		record on the seed (no-op). Safe even after the join has succeeded —
		but `_cold_join_done` short-circuits once a peer reply was observed,
		so the re-sends stop."""
		if self._cold_join_done:
			return
		if self.cold_join_seeds is None:
			# `main.py` didn't wire the retry (e.g., a test loop). The
			# one-shot `cold_join` from `main()` already ran; nothing more
			# to do here.
			self._cold_join_done = True
			return
		if not self.cold_join_seeds:
			# No seeds configured (lone-host posture per §9.2). Nothing to
			# retry; come up peer-empty and let later gossip/anti-entropy
			# fill in.
			self._cold_join_done = True
			return
		if self._cold_join_attempts >= self.COLD_JOIN_MAX_ATTEMPTS:
			# Cap reached — give up. The regular gossip/anti-entropy loop
			# keeps running; if a seed was just slow to reply it will reach
			# us through that path. The risk of permanent partition is low
			# (15 datagrams over 30 s on a UDP path that drops ALL of them is
			# a sign the network is broken, not a transient loss).
			self._cold_join_done = True
			return
		if now < self._next_cold_join_at:
			return
		self._next_cold_join_at = now + self.COLD_JOIN_RETRY_INTERVAL
		self._cold_join_attempts += 1
		t = self.daemon.transport
		if t is None:
			return
		from .join import cold_join

		cold_join(self.daemon, t, self.cold_join_seeds)

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
		changed = False  # any state mutation across membership + ownership

		# 0) §14.4 — a host that advertised `kind=leaving` is fast-pathed
		# alive → dead (skipping `suspect`) once `leaving_grace` elapses. Run
		# this BEFORE `tracker.gc` so a just-promoted host lands on the normal
		# `dead_grace`/`ownership_grace`/`routable_dead` ladder this same tick
		# (its `dead_at` is stamped at `now`). A refute that arrived within the
		# grace already cleared `leaving_at` (via `note_alive`), so it is not
		# promoted here.
		if tracker.promote_leaving_if_due(self.daemon.config.leaving_grace):
			changed = True

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
		if reaped:
			changed = True
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
				changed = True
				if ownership_reaped:
					self.apply.schedule(now, self.daemon.config.apply_debounce)
				# Pop `dead_at` only once `ownership_grace` has elapsed so a
				# late refuter (§14.3) doesn't lose its routes mid-window.
				if now - dead_at >= self.daemon.config.ownership_grace:
					tracker.dead_at.pop(host_id, None)
					tracker.peers.pop(host_id, None)
		if changed:
			# Persist the reaped state so a crash-restart doesn't bring
			# dead peers back from a stale state.json.
			save_state(self.daemon.state, self.daemon.config.data_dir)
			if reaped:
				# A membership was reaped — schedule the apply so the peer
				# disappears from WgDesired's peer set.
				self.apply.schedule(now, self.daemon.config.apply_debounce)

	def _rate_limiter_for(self) -> SourceRateLimiter:
		"""Lazily build the §19 per-source rate limiter from `daemon.config` on
		first use (a bare test Loop needn't pre-wire it). Rebuilt only if never
		built — the config is frozen for the daemon's lifetime."""
		if self._rate_limiter is None:
			cfg = self.daemon.config
			self._rate_limiter = SourceRateLimiter(
				limit=cfg.inbound_rate_limit,
				window=cfg.inbound_rate_window,
				max_sources=cfg.inbound_rate_max_sources,
				now_fn=self.now_fn,
			)
		return self._rate_limiter

	def _drain_incoming(self) -> None:
		"""Spec §13.2 — recv pending UDP datagrams (non-blocking) and dispatch
		through the same `handle_message` that join replies use. A freshly-applied
		record marks the apply as needing a re-render (the renderer reads the
		effective table from `state` next apply round).

		M2 flood defense (§19): ANCP is plaintext public UDP reachable from the
		IPv6 internet, and each datagram costs an ed25519 envelope verify on this
		single-threaded loop. Two bounds keep a remote flood from starving
		gossip/probe/apply:
		  - a per-source rate limit (`pre_filter`) checked on the RAW address
		    BEFORE parse/verify — an abusive source is dropped for the ed25519
		    cost of nothing; each drop bumps `inbound_rate_limited`.
		  - a per-tick drain budget (`inbound_tick_budget`) — at most that many
		    datagrams are verified+dispatched per tick; excess is left in the
		    kernel buffer and the loop still runs its other work. When the budget
		    caps the tick (the socket still had data), `inbound_budget_exhausted`
		    is bumped.

		Stage 5+ — the envelope signature (§19.1) is verified BEFORE any
		payload work; a datagram whose envelope fails to verify is dropped +
		counted (`envelope_signature_failed`). The verify runs only if
		`daemon.envelope_verifier` is installed (production path; tests that
		build unsigned envelopes leave it unset)."""
		t = self.daemon.transport
		if t is None:
			return

		counter = getattr(self.daemon, "metrics", None)
		limiter = self._rate_limiter_for()

		def _pre_filter(addr) -> bool:
			# addr is (host, port, flowinfo, scopeid) for AF_INET6; key on the
			# source IP only so all ports from one host share a bucket.
			ip = addr[0] if addr else ""
			if limiter.allow(ip):
				return True
			if counter is not None:
				counter.incr("inbound_rate_limited")
			return False

		def _on_msg(msg, sender_addr) -> None:
			verifier = getattr(self.daemon, "envelope_verifier", None)
			if verifier is not None:
				try:
					verifier(msg, self.daemon)
				except Exception:
					if counter is not None:
						counter.incr("envelope_signature_failed")
					return  # drop silently — loop stays alive
			handle_message(msg, sender_addr, self.daemon, self.gossip_state)
			# We received a verified peer datagram → our cold-join succeeded
			# (the §9.2 retry in `_cold_join_if_due` can stop re-sending). A
			# seed's cold-join reply arrives as a TYPE_GOSSIP bundle here; the
			# envelope verifier + handler both passing means we are now in the
			# cluster. Subsequent cold-join attempts would just re-apply the
			# same MembershipRecord on the seed — no-op, but pointless.
			if not self._cold_join_done:
				self._cold_join_done = True
			# A membership change (someone joined) or ownership change (a peer
			# advertised new /128s) potentially changes our desired wg-mesh
			# config. Schedule a debounced apply so we re-render + syncconf.
			self.apply.schedule(self._now(), self.daemon.config.apply_debounce)

		budget = self.daemon.config.inbound_tick_budget
		dispatched = t.drain(_on_msg, max_datagrams=budget, pre_filter=_pre_filter)
		if counter is not None and budget is not None and dispatched >= budget:
			# We hit the per-tick budget with data still likely pending — a flood
			# (or a legitimate burst). Count it so an operator can distinguish a
			# starved tick from a quiet one; the excess drains next tick / the
			# kernel drops it. Bumped only when the cap actually bites.
			counter.incr("inbound_budget_exhausted")


__all__ = ["Loop", "ScheduledApply"]
