"""Unit tests for Stage 4 — the SWIM probe protocol + observer-local failure
tracker + leave-advertise hook (spec §14). Pure: injected clock, injected
transport backed by the same in-memory Bus shape as Stages 2/3.

Coverage:
  - FailureTracker transitions (alive → suspect → dead; note_alive refute)
  - GC reaping of dead memberships + ownership advertisements
  - ProbeProtocol round-shape (direct pings, ack matching, extended-deadline
    indirect relays)
  - Ack correlation (direct + relayed-ack forwarding)
  - Indirect ping forwarding
  - Refute trigger from a higher-gen Membership Record clears suspect
  - Round-trip: A pings B; B doesn't ack within timeout; A marks B suspect;
    B's next Membership Advertisement gen+1 refutes via the gossip apply rule
"""

import random
import tempfile
import time
import unittest
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from atlas.networkd import wire
from atlas.networkd.config import Config
from atlas.networkd.daemon import Daemon, build_initial
from atlas.networkd.failure import FailureState, FailureTracker, PeerFailureState
from atlas.networkd.gossip import GossipState, handle_message
from atlas.networkd.identity import HostIdentity
from atlas.networkd.probe import ProbeProtocol
from atlas.networkd.records import (
	MembershipKind,
	MembershipRecord,
	MemberState,
	owning_advertisement,
)
from atlas.networkd.state import AppliedState
from atlas.networkd.wire import (
	TYPE_ACK,
	TYPE_INDIRECT_PING,
	TYPE_PING,
	Message,
	from_bytes,
)

# --- helpers (parallel to Stages 2/3's test helpers) ------------------------


def member(host_id: str, gen: int, key: str = "k", mesh: str | None = None) -> MembershipRecord:
	return MembershipRecord(
		host_id=host_id,
		kind=MembershipKind.MEMBER,
		state=MemberState.ALIVE,
		endpoint=f"2001:db9::{host_id}",
		wg_public_key=key,
		mesh_address=mesh or f"fdaa:0:0:{host_id}::1",
		generation=gen,
	)


def leaving_member(host_id: str, gen: int, key: str = "k", mesh: str | None = None) -> MembershipRecord:
	"""A graceful-shutdown Membership Record (§14.4) — `kind=leaving`, wire
	`state=leaving`. This is what `main._advertise_leaving` emits on SIGTERM."""
	return MembershipRecord(
		host_id=host_id,
		kind=MembershipKind.LEAVING,
		state=MemberState.LEAVING,
		endpoint=f"2001:db9::{host_id}",
		wg_public_key=key,
		mesh_address=mesh or f"fdaa:0:0:{host_id}::1",
		generation=gen,
	)


def ownership(origin: str, gen: int, *ips: str):
	return owning_advertisement(origin=origin, generation=gen, owned=ips)


def _daemon(host_id: str) -> Daemon:
	"""A test Daemon: tmpdir-backed data_dir, all host seams stubbed, transport
	+ tracker + probe_protocol left to the test to wire."""
	data_dir = tempfile.mkdtemp(prefix="atlas-networkd-test-")
	cfg = Config().with_overrides(data_dir=data_dir)
	ident = HostIdentity(
		host_id=host_id,
		endpoint=f"2001:db9::{host_id}",
		mesh_address=f"fdaa:0:0:{host_id}::1",
	)
	state = AppliedState()
	daemon = build_initial(ident, cfg, state, public_key=f"PUB-{host_id}")
	daemon.run = lambda *a, **kw: ""  # type: ignore[method-assign]
	daemon.write_run_config = lambda body: None  # type: ignore[method-assign]
	return daemon


@dataclass(slots=True)
class FakeTransport:
	bind: tuple[str, int]
	bus: "Bus" = field(default_factory=lambda: Bus())
	socket: object | None = field(default=object())

	def start(self) -> None:
		_ = self

	def stop(self) -> None:
		self.socket = None

	def send(self, target: tuple[str, int], data: bytes) -> None:
		self.bus._send(self.bind, target, data)

	def drain(self, handler: Callable) -> int:
		count = 0
		while True:
			entry = self.bus._recv(self.bind)
			if entry is None:
				break
			data, sender = entry
			count += 1
			try:
				msg = from_bytes(data)
			except ValueError:
				continue
			handler(msg, sender)
		return count


@dataclass
class Bus:
	queues: dict[tuple[str, int], list[tuple[bytes, tuple[str, int]]]] = field(
		default_factory=lambda: defaultdict(list)
	)

	def _send(self, source: tuple[str, int], target: tuple[str, int], data: bytes) -> None:
		self.queues[target].append((data, source))

	def _recv(self, me: tuple[str, int]) -> tuple[bytes, tuple[str, int]] | None:
		q = self.queues.get(me)
		if not q:
			return None
		return q.pop(0)


# --- FailureTracker ---------------------------------------------------------


class TestFailureTrackerTransitions(unittest.TestCase):
	def test_unknown_peer_starts_alive(self):
		t = FailureTracker(now_fn=lambda: 0.0)
		self.assertEqual(t.state_of("h2"), FailureState.ALIVE)

	def test_mark_suspect_ladder(self):
		clock = [0.0]
		t = FailureTracker(now_fn=lambda: clock[0])
		t.mark_suspect("h2")
		self.assertEqual(t.state_of("h2"), FailureState.SUSPECT)
		self.assertEqual(t.peers["h2"].since, 0.0)

	def test_note_alive_refute_resets(self):
		clock = [0.0]
		t = FailureTracker(now_fn=lambda: clock[0])
		t.mark_suspect("h2")
		clock[0] = 5.0
		t.note_alive("h2")
		self.assertEqual(t.state_of("h2"), FailureState.ALIVE)
		self.assertEqual(t.peers["h2"].since, 5.0)

	def test_mark_dead_idempotent(self):
		t = FailureTracker(now_fn=lambda: 0.0)
		t.mark_dead("h2")
		t.mark_dead("h2")  # idempotent
		self.assertEqual(t.state_of("h2"), FailureState.DEAD)
		# `dead_at` is set exactly once (would be the same value either way).
		self.assertEqual(t.dead_at, {"h2": 0.0})

	def test_note_alive_clears_dead_at_timer(self):
		# A peer declared dead that refutes (via a fresh alive Membership
		# Advertisement) — the GC timer is disarmed so we don't reap a host that
		# just proved it's alive.
		clock = [0.0]
		t = FailureTracker(now_fn=lambda: clock[0])
		t.mark_dead("h2")
		clock[0] = 3.0
		t.note_alive("h2")
		self.assertEqual(t.state_of("h2"), FailureState.ALIVE)
		self.assertNotIn("h2", t.dead_at)

	def test_suspect_doesnt_reapply_to_dead_peer(self):
		# A dead peer that fails a probe stays dead — probe failure doesn't
		# walk back into the ladder.
		t = FailureTracker(now_fn=lambda: 0.0)
		t.mark_dead("h2")
		t.mark_suspect("h2")  # should be a no-op
		self.assertEqual(t.state_of("h2"), FailureState.DEAD)


# --- GC ---------------------------------------------------------


class TestGarbageCollection(unittest.TestCase):
	def test_dead_membership_reaped_after_dead_grace(self):
		# Mark h2 dead at t=0; advance clock past `dead_grace`; GC reaps
		# membership but keeps `dead_at`/`peers` alive for the loop's
		# ownership-reap step (§14.3 — routes outlast the membership window).
		clock = [0.0]
		t = FailureTracker(now_fn=lambda: clock[0])
		state = AppliedState()
		state.apply_membership(member("h2", 1))
		t.mark_dead("h2")
		# Before `dead_grace` → no reap.
		reaped = t.gc(suspect_timeout=999.0, dead_grace=10.0, ownership_grace=20.0, state=state)
		self.assertEqual(reaped, [])
		self.assertIn("h2", state.membership)
		# Advance past `dead_grace` → membership reaped; `dead_at`/`peers`
		# kept so the loop can still reap ownership past `ownership_grace`.
		clock[0] = 11.0
		reaped = t.gc(suspect_timeout=999.0, dead_grace=10.0, ownership_grace=20.0, state=state)
		self.assertEqual(reaped, ["h2"])
		self.assertNotIn("h2", state.membership)
		self.assertIn("h2", t.dead_at)  # kept for ownership-reap window
		self.assertIn("h2", t.peers)  # kept for ownership-reap window

	def test_ownership_reaped_after_ownership_grace(self):
		# A dead host's ownership advertisement stays past `dead_grace` to give
		# it a refute window; reaped only after `ownership_grace`.
		clock = [0.0]
		t = FailureTracker(now_fn=lambda: clock[0])
		state = AppliedState()
		state.apply_ownership(ownership("h2", 1, "fdaa::1"))
		t.mark_dead("h2")
		# At dead_grace=10 — membership reaped but ownership stays.
		clock[0] = 11.0
		t.gc(suspect_timeout=999.0, dead_grace=10.0, ownership_grace=20.0, state=state)
		self.assertNotIn("h2", state.membership)
		self.assertIn("h2", state.ownership)  # routes preserved
		# Advance past `ownership_grace` → ownership reaped via the loop's
		# explicit `gc_origin_if_dead` call.
		clock[0] = 21.0
		reaped = state.gc_origin_if_dead("h2", dead_at=0.0, ownership_grace=20.0, now=clock[0])
		self.assertTrue(reaped)
		self.assertNotIn("h2", state.ownership)

	def test_suspect_promoted_to_dead_after_suspect_timeout(self):
		# A suspect peer whose `suspect_timeout` elapsed is promoted to dead
		# by gc, then reaped after `dead_grace` (§14.3 ladder).
		clock = [0.0]
		t = FailureTracker(now_fn=lambda: clock[0])
		state = AppliedState()
		state.apply_membership(member("h2", 1))
		t.mark_suspect("h2")
		# Before suspect_timeout → still suspect, not dead.
		clock[0] = 4.0
		t.gc(suspect_timeout=5.0, dead_grace=10.0, ownership_grace=20.0, state=state)
		self.assertEqual(t.state_of("h2"), FailureState.SUSPECT)
		self.assertNotIn("h2", t.dead_at)
		# Advance past suspect_timeout → promoted to dead.
		clock[0] = 6.0
		t.gc(suspect_timeout=5.0, dead_grace=10.0, ownership_grace=20.0, state=state)
		self.assertEqual(t.state_of("h2"), FailureState.DEAD)
		self.assertIn("h2", t.dead_at)
		# Advance past dead_grace → reaped.
		clock[0] = 17.0
		reaped = t.gc(suspect_timeout=5.0, dead_grace=10.0, ownership_grace=20.0, state=state)
		self.assertIn("h2", reaped)
		self.assertNotIn("h2", state.membership)

	def test_ownership_then_dead_at_cleared_after_ownership_grace(self):
		# Full GC lifecycle: mark dead → membership reaped at dead_grace →
		# ownership reaped at ownership_grace → dead_at/peers cleared.
		clock = [0.0]
		t = FailureTracker(now_fn=lambda: clock[0])
		state = AppliedState()
		state.apply_membership(member("h2", 1))
		state.apply_ownership(ownership("h2", 1, "fdaa::1"))
		t.mark_dead("h2")
		# At t=11 (past dead_grace=10): membership reaped, ownership stays.
		clock[0] = 11.0
		t.gc(suspect_timeout=999.0, dead_grace=10.0, ownership_grace=20.0, state=state)
		self.assertNotIn("h2", state.membership)
		self.assertIn("h2", state.ownership)
		self.assertIn("h2", t.dead_at)  # kept for ownership-reap window
		# At t=21 (past ownership_grace=20): ownership reaped via the loop's
		# gc_origin_if_dead; dead_at/peers cleared (simulates the loop's step 2).
		clock[0] = 21.0
		state.gc_origin_if_dead("h2", dead_at=0.0, ownership_grace=20.0, now=clock[0])
		t.dead_at.pop("h2", None)
		t.peers.pop("h2", None)
		self.assertNotIn("h2", state.ownership)
		self.assertNotIn("h2", t.dead_at)
		self.assertNotIn("h2", t.peers)

	def test_ownership_grace_strictly_longer_than_dead_grace(self):
		# The spec §14.3 invariant: ownership_grace > dead_grace so a host that
		# refutes late (within ownership_grace) doesn't lose its routes. Check
		# the Config defaults reflect it.
		from atlas.networkd.config import Config

		c = Config()
		self.assertGreater(c.ownership_grace, c.dead_grace)


# --- H7: routes survive to ownership_grace, not dead_grace -------------------


class TestDeadPeerRoutesSurviveOwnershipGrace(unittest.TestCase):
	"""H7 — a dead host's `[Peer]` (endpoint, key, mesh_address) must SURVIVE
	for rendering as long as its Ownership Records do (until `ownership_grace`),
	not vanish at `dead_grace` when its membership is reaped. Before the fix the
	/128s blackholed ~`ownership_grace - dead_grace` s early because the reaped
	membership left no `[Peer]` to carry them."""

	def _daemon_with_dead_owner(self):
		"""Build a rendering daemon (self=h1) that has learned a peer h2 which
		owns a /128, then declares h2 dead at t=0 via the tracker. Returns
		(daemon, tracker, clock, the /128 h2 owns)."""
		daemon = _daemon("h1")
		clock = [0.0]
		tracker = FailureTracker(now_fn=lambda: clock[0])
		daemon.failure_tracker = tracker
		owned = "fdaa:1::42"
		daemon.state.apply_membership(member("h2", 1))
		daemon.state.apply_ownership(ownership("h2", 1, owned))
		tracker.mark_dead("h2")
		return daemon, tracker, clock, owned

	def _run_gc(self, daemon, tracker, clock) -> None:
		"""Drive the GC lifecycle the way the loop's `_gc_if_due` does: reap
		membership at dead_grace (moving still-owning hosts to routable_dead),
		then reap ownership + clear the render entry past ownership_grace."""
		cfg = daemon.config
		tracker.gc(cfg.suspect_timeout, cfg.dead_grace, cfg.ownership_grace, daemon.state)
		for host_id in list(tracker.dead_at.keys()):
			dead_at = tracker.dead_at[host_id]
			daemon.state.gc_origin_if_dead(
				host_id, dead_at=dead_at, ownership_grace=cfg.ownership_grace, now=clock[0]
			)
			if clock[0] - dead_at >= cfg.ownership_grace:
				tracker.dead_at.pop(host_id, None)
				tracker.peers.pop(host_id, None)

	def test_routes_present_while_dead_but_within_ownership_grace(self):
		# dead_grace=30, ownership_grace=60 (defaults). At t=40 h2's membership is
		# reaped but its ownership survives → its /128 must STILL be in a [Peer].
		daemon, tracker, clock, owned = self._daemon_with_dead_owner()
		clock[0] = 40.0  # past dead_grace (30), within ownership_grace (60)
		self._run_gc(daemon, tracker, clock)
		self.assertNotIn("h2", daemon.state.membership)  # membership reaped
		self.assertIn("h2", daemon.state.routable_dead)  # kept render-only
		out = daemon.render_current()
		self.assertIn("PublicKey = k", out)  # h2's [Peer] still rendered
		self.assertIn(f"{owned}/128", out)  # its owned /128 still routes
		self.assertIn("fdaa:0:0:h2::1/128", out)  # its mesh /128 too

	def test_routes_gone_after_ownership_grace(self):
		# At t=61 both membership AND ownership are reaped → no [Peer], no /128.
		daemon, tracker, clock, owned = self._daemon_with_dead_owner()
		clock[0] = 40.0
		self._run_gc(daemon, tracker, clock)  # reap membership first (dead_grace)
		clock[0] = 61.0  # past ownership_grace (60)
		self._run_gc(daemon, tracker, clock)
		self.assertNotIn("h2", daemon.state.routable_dead)
		out = daemon.render_current()
		self.assertNotIn(f"{owned}/128", out)
		self.assertNotIn("fdaa:0:0:h2::1/128", out)

	def test_dead_owner_that_owns_nothing_reaped_at_dead_grace(self):
		# A dead host with NO ownership records is reaped outright at dead_grace —
		# we don't leak a render-only [Peer] for a host with nothing to route.
		daemon = _daemon("h1")
		clock = [0.0]
		tracker = FailureTracker(now_fn=lambda: clock[0])
		daemon.failure_tracker = tracker
		daemon.state.apply_membership(member("h2", 1))  # no ownership for h2
		tracker.mark_dead("h2")
		clock[0] = 40.0  # past dead_grace
		self._run_gc(daemon, tracker, clock)
		self.assertNotIn("h2", daemon.state.membership)
		self.assertNotIn("h2", daemon.state.routable_dead)  # not leaked
		self.assertNotIn("PublicKey = k", daemon.render_current())

	def test_late_refute_repopulates_and_clears_routable_dead(self):
		# A host that refutes late (§14.5) — a higher-gen alive Membership Record
		# — repopulates `membership` and drops the stale render-only record; it
		# is not double-carried.
		daemon, tracker, clock, owned = self._daemon_with_dead_owner()
		clock[0] = 40.0
		self._run_gc(daemon, tracker, clock)
		self.assertIn("h2", daemon.state.routable_dead)
		# h2 refutes with gen 2.
		daemon.state.apply_membership(member("h2", 2))
		tracker.note_alive("h2")
		self.assertIn("h2", daemon.state.membership)
		self.assertNotIn("h2", daemon.state.routable_dead)  # stale copy dropped
		out = daemon.render_current()
		self.assertEqual(out.count(f"{owned}/128"), 1)  # rendered exactly once

	def test_dead_peer_excluded_from_gossip_peer_selection(self):
		# The dead host must not be gossiped-to / anti-entropy'd / probed: those
		# read `state.membership`, from which it was reaped. Only render sees it.
		from atlas.networkd.peers import select_peers

		daemon, tracker, clock, _ = self._daemon_with_dead_owner()
		clock[0] = 40.0
		self._run_gc(daemon, tracker, clock)
		peers = select_peers(daemon.state.membership, "h1", count=5)
		self.assertNotIn("h2", peers)


# --- ProbeProtocol: round + ack matching ------------------------------------


class TestProbeProtocol(unittest.TestCase):
	def _wired_daemons(self, ids: list[str]) -> tuple[dict, Bus]:
		"""Construct `len(ids)` test daemons with the same FakeTransport/Bus
		mesh, each wired with FailureTracker + ProbeProtocol sharing the
		injected clock."""
		bus = Bus()
		clock = [0.0]
		daemons: dict[str, Daemon] = {}
		for host_id in ids:
			d = _daemon(host_id)
			t = FakeTransport(bind=(d.identity.endpoint, 7946), bus=bus)
			t.socket = object()
			d.transport = t
			tracker = FailureTracker(now_fn=lambda c=clock: c[0])
			d.failure_tracker = tracker
			probe = ProbeProtocol(tracker=tracker, config=d.config, now_fn=lambda c=clock: c[0])
			d.probe_protocol = probe
			daemons[host_id] = d
		# Cross-install everyone's Membership Record so probes can find targets.
		for hd, d in daemons.items():
			for other_id, other in daemons.items():
				if other_id != hd:
					d.state.apply_membership(other.own_membership)
		return daemons, bus

	def test_probe_round_sends_pings_to_selected_peers(self):
		daemons, bus = self._wired_daemons(["h1", "h2", "h3", "h4"])
		h1 = daemons["h1"]
		sent = h1.probe_protocol.probe_round(h1, h1.transport, nonces=iter([101, 102, 103]))
		self.assertEqual(sent, 3)
		# Three PING datagrams on the bus, addressed to three distinct peers.
		ping_targets: list[str] = []
		for (target_addr, _port), q in bus.queues.items():
			for data, _ in q:
				msg = from_bytes(data)
				if msg.type == TYPE_PING:
					ping_targets.append(target_addr[0])
		self.assertEqual(len(ping_targets), 3)

	def test_ack_clears_in_flight_and_marks_alive(self):
		daemons, _bus = self._wired_daemons(["h1", "h2"])
		h1 = daemons["h1"]
		# h1 suspects h2 (pre-existing).
		h1.failure_tracker.mark_suspect("h2")
		# h1 sends h2 a ping (nonce 999); h2 acks; h1's tracker is reset.
		h1.probe_protocol.in_flight[999] = ("h2", 0.0 + h1.config.probe_timeout)
		ack = Message(type=TYPE_ACK, sender="h2", payload=wire.ack_payload(999, "h2"))
		h1.probe_protocol.handle_ack(ack, h1, h1.transport)
		self.assertNotIn(999, h1.probe_protocol.in_flight)
		self.assertEqual(h1.failure_tracker.state_of("h2"), FailureState.ALIVE)

	def test_ack_for_unknown_nonce_dropped(self):
		# A late ack for a nonce we've already forgotten — drop, no state change.
		daemons, _bus = self._wired_daemons(["h1", "h2"])
		h1 = daemons["h1"]
		ack = Message(type=TYPE_ACK, sender="h2", payload=wire.ack_payload(12345, "h2"))
		h1.probe_protocol.handle_ack(ack, h1, h1.transport)
		# Nothing changes — no in_flight entry to clear, no crash.
		self.assertEqual(h1.probe_protocol.in_flight, {})

	def test_check_timeouts_extends_then_marks_suspect(self):
		# Inject nonces so probe_round uses them; let the ping miss; first call
		# to check_timeouts extends (sends indirect pings + re-arms); second
		# call after `indirect_timeout` marks suspect.
		daemons, bus = self._wired_daemons(["h1", "h2", "h3", "h4"])
		h1 = daemons["h1"]
		# Override `probe_peers` to 1 so a single nonce targets h2 deterministically
		# (h2 is the first random pick — the seeded select_peers returns it).
		# We use an infinite nonce iterator and rely on the count.
		from itertools import count

		nonce_iter = count(777)
		# Force probe_peers=1 by mutating the config on h1's probe_protocol.
		h1.probe_protocol.config = h1.config.with_overrides(probe_peers=1)
		sent = h1.probe_protocol.probe_round(h1, h1.transport, nonces=nonce_iter)
		self.assertEqual(sent, 1)
		# No ack arrives. Advance past `probe_timeout`; check_timeouts extends
		# (sends K indirect pings to alive peers other than the target). We
		# re-bind h1's tracker + probe clocks to a clock we control directly.
		clock = [0.0 + h1.config.probe_timeout + 0.01]  # noqa: F841  (clock kept for clarity)
		from atlas.networkd.failure import FailureTracker as FT

		clock2 = [100.0]
		h1.failure_tracker.now_fn = lambda c=clock2: c[0]  # type: ignore[method-assign]
		h1.probe_protocol.now_fn = lambda c=clock2: c[0]  # type: ignore[method-assign]
		# Manually arm an in_flight ping at the present clock.
		h1.probe_protocol.in_flight.clear()
		h1.probe_protocol.in_flight[777] = ("h2", clock2[0] + 0.5)  # probe_timeout=0.5
		# First miss: past probe_timeout.
		clock2[0] = clock2[0] + 0.6
		marked = h1.probe_protocol.check_timeouts(h1, h1.transport)
		self.assertEqual(marked, [])  # not yet — extended
		self.assertIn(777, h1.probe_protocol._extended_nonces)
		# Indirect pings were sent: a queue entry exists for each relay (3 by
		# default; we have 2 other alive peers h3, h4 → 2 relay sends).
		indirect_targets: list[str] = []
		for (target_addr, _), q in bus.queues.items():
			for data, _ in q:
				msg = from_bytes(data)
				if msg.type == TYPE_INDIRECT_PING:
					indirect_targets.append(target_addr[0])
		self.assertGreater(len(indirect_targets), 0)
		# Second miss: past indirect_timeout.
		clock2[0] = clock2[0] + h1.config.indirect_timeout + 0.01
		marked = h1.probe_protocol.check_timeouts(h1, h1.transport)
		self.assertEqual(marked, ["h2"])
		self.assertEqual(h1.failure_tracker.state_of("h2"), FailureState.SUSPECT)


# --- Refute trigger from a higher-gen Membership Record -------------------


class TestRefuteTrigger(unittest.TestCase):
	def test_higher_gen_membership_record_clears_suspect(self):
		# h1 has h2 in `suspect`. h2 propagates a fresh Membership Record
		# (gossip piggyback) at gen=current+1; the §10.3 apply rule accepts it;
		# the §14 fast-refute trigger (wired in gossip._apply_record) calls
		# FailureTracker.note_alive → h2's ladder resets to alive.
		daemons, _bus = self._wired_for_refute()
		h1, h2 = daemons["h1"], daemons["h2"]
		# h1 has h2 at gen 1 (from cross-install); mark h2 suspect.
		h1.failure_tracker.mark_suspect("h2")
		self.assertEqual(h1.failure_tracker.state_of("h2"), FailureState.SUSPECT)
		# h2 bumps its own Generation + re-issues its Membership Record.
		h2.state.bump_own_generation()
		new_h2_record = MembershipRecord(
			host_id="h2",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::h2",
			wg_public_key=h2.own_membership.wg_public_key,
			mesh_address="fdaa:0:0:h2::1",
			generation=h2.state.own_generation,
		)
		# Ship the new record to h1 via a Gossip message; h1 applies it via
		# `handle_message` → `_apply_record` → `note_alive` trigger.
		msg = Message(
			type="gossip",
			sender="h2",
			payload=wire.gossip_payload([new_h2_record]),
		)
		handle_message(msg, ("2001:db9::h2", 7946), h1, GossipState())
		self.assertEqual(h1.failure_tracker.state_of("h2"), FailureState.ALIVE)
		# And h1's stored Membership Record for h2 carries the higher gen.
		self.assertEqual(h1.state.membership["h2"].generation, h2.state.own_generation)

	def _wired_for_refute(self) -> tuple[dict, Bus]:
		# Two hosts; h1 has h2 already suspect-marked (the test does the
		# marking); h2 will refute via a fresh-gen Membership Record.
		bus = Bus()
		clock = [0.0]
		daemons: dict[str, Daemon] = {}
		for host_id in ("h1", "h2"):
			d = _daemon(host_id)
			t = FakeTransport(bind=(d.identity.endpoint, 7946), bus=bus)
			t.socket = object()
			d.transport = t
			tracker = FailureTracker(now_fn=lambda c=clock: c[0])
			d.failure_tracker = tracker
			probe = ProbeProtocol(tracker=tracker, config=d.config, now_fn=lambda c=clock: c[0])
			d.probe_protocol = probe
			daemons[host_id] = d
		for hd, d in daemons.items():
			for other_id, other in daemons.items():
				if other_id != hd:
					d.state.apply_membership(other.own_membership)
		return daemons, bus


# --- Indirect ping relay forwards ack to requester -----------------------


class TestIndirectRelay(unittest.TestCase):
	def test_relay_forwards_ack_to_requester(self):
		# h1 sends IND_PING(target=h3, requester=h1) to relay h2.
		# h2 sends PING(nonce, h3) to h3.
		# h3 acks h2 with that nonce.
		# h2 forwards the ACK to h1.
		# h1's `in_flight[nonce]` should match.
		daemons, bus = self._wired_three()
		h1, h2, h3 = daemons["h1"], daemons["h2"], daemons["h3"]
		# h1 arms an in_flight ping to h3 with nonce 5050.
		h1.probe_protocol.in_flight[5050] = ("h3", 0.0 + 60.0)
		# h1 emits IND_PING to relay h2 (manually, bypassing the round).
		ind_msg = Message(
			type=TYPE_INDIRECT_PING,
			sender="h1",
			payload=wire.indirect_ping_payload(5050, "h3", "h1"),
		)
		h1.unicast_send(h2.identity.endpoint, ind_msg.to_bytes())
		# h2 drains the IND_PING and forwards a PING to h3.
		h2.probe_protocol  # ensure wired
		h2_t = h2.transport
		h2_t.drain(lambda msg, addr: handle_message(msg, addr, h2, GossipState()))
		# Confirm a PING landed on h3's queue.
		h3_msgs = [from_bytes(data) for data, _ in bus.queues.get((h3.identity.endpoint, 7946), [])]
		self.assertTrue(any(m.type == TYPE_PING for m in h3_msgs))
		# h3 drains the PING; emits an ACK with the same nonce to h2.
		h3_t = h3.transport
		h3_t.drain(lambda msg, addr: handle_message(msg, addr, h3, GossipState()))
		# h2 drains the ACK; forwards it to h1.
		h2_t.drain(lambda msg, addr: handle_message(msg, addr, h2, GossipState()))
		# h1 drains the forwarded ACK; `in_flight[5050]` is cleared and h3 is
		# `alive` (the fast-refute trigger).
		h1_t = h1.transport
		h1_t.drain(lambda msg, addr: handle_message(msg, addr, h1, GossipState()))
		self.assertNotIn(5050, h1.probe_protocol.in_flight)
		self.assertEqual(h1.failure_tracker.state_of("h3"), FailureState.ALIVE)

	def _wired_three(self) -> tuple[dict, Bus]:
		bus = Bus()
		clock = [0.0]
		daemons: dict[str, Daemon] = {}
		for host_id in ("h1", "h2", "h3"):
			d = _daemon(host_id)
			t = FakeTransport(bind=(d.identity.endpoint, 7946), bus=bus)
			t.socket = object()
			d.transport = t
			tracker = FailureTracker(now_fn=lambda c=clock: c[0])
			d.failure_tracker = tracker
			probe = ProbeProtocol(tracker=tracker, config=d.config, now_fn=lambda c=clock: c[0])
			d.probe_protocol = probe
			daemons[host_id] = d
		for hd, d in daemons.items():
			for other_id, other in daemons.items():
				if other_id != hd:
					d.state.apply_membership(other.own_membership)
		return daemons, bus


# --- H6: graceful `leaving` fast-paths alive → dead (§14.3 / §14.4) ----------


class TestLeavingFastPath(unittest.TestCase):
	"""H6 — a `kind=leaving` graceful-shutdown record must NOT resurrect the
	origin to ALIVE (the inverted `note_alive` bug); instead it arms a
	`leaving_grace` countdown that marks the origin `dead` DIRECTLY (skipping
	`suspect`). A sub-`leaving_grace` `alive` refute cancels the countdown."""

	def _daemon_with_tracker(self, host_id: str = "h1"):
		daemon = _daemon(host_id)
		clock = [0.0]
		tracker = FailureTracker(now_fn=lambda: clock[0])
		daemon.failure_tracker = tracker
		return daemon, tracker, clock

	# (a) applying a leaving record does NOT mark the origin ALIVE, and does NOT
	# clear an existing suspicion into alive.
	def test_leaving_record_does_not_mark_origin_alive(self):
		daemon, tracker, _clock = self._daemon_with_tracker()
		daemon.state.apply_membership(member("h2", 1))
		# h2 gracefully leaves at gen 2.
		msg = Message(type="gossip", sender="h2", payload=wire.gossip_payload([leaving_member("h2", 2)]))
		handle_message(msg, ("2001:db9::h2", 7946), daemon, GossipState())
		# The origin is NOT reset to alive; the leaving countdown is armed.
		self.assertIn("h2", tracker.leaving_at)
		self.assertNotIn("h2", tracker.dead_at)  # not dead yet (grace not elapsed)

	def test_leaving_record_does_not_clear_existing_suspicion(self):
		# The inverted-note_alive bug: a leaving record on a SUSPECT peer would
		# have flipped it back to alive. It must stay suspect (and additionally
		# arm the leaving countdown).
		daemon, tracker, _clock = self._daemon_with_tracker()
		daemon.state.apply_membership(member("h2", 1))
		tracker.mark_suspect("h2")
		msg = Message(type="gossip", sender="h2", payload=wire.gossip_payload([leaving_member("h2", 2)]))
		handle_message(msg, ("2001:db9::h2", 7946), daemon, GossipState())
		self.assertEqual(tracker.state_of("h2"), FailureState.SUSPECT)
		self.assertIn("h2", tracker.leaving_at)

	# (b) after leaving_grace elapses the origin is marked dead WITHOUT passing
	# through suspect.
	def test_leaving_promoted_to_dead_skipping_suspect(self):
		daemon, tracker, clock = self._daemon_with_tracker()
		daemon.state.apply_membership(member("h2", 1))
		msg = Message(type="gossip", sender="h2", payload=wire.gossip_payload([leaving_member("h2", 2)]))
		handle_message(msg, ("2001:db9::h2", 7946), daemon, GossipState())
		# Before leaving_grace (2 s default) → still alive, still counting down.
		clock[0] = 1.0
		promoted = tracker.promote_leaving_if_due(daemon.config.leaving_grace)
		self.assertEqual(promoted, [])
		self.assertEqual(tracker.state_of("h2"), FailureState.ALIVE)
		# The peer NEVER entered suspect on the way to dead.
		self.assertNotEqual(tracker.state_of("h2"), FailureState.SUSPECT)
		# Past leaving_grace → dead directly, and dead_at armed for the normal
		# dead_grace/ownership_grace ladder.
		clock[0] = 3.0
		promoted = tracker.promote_leaving_if_due(daemon.config.leaving_grace)
		self.assertEqual(promoted, ["h2"])
		self.assertEqual(tracker.state_of("h2"), FailureState.DEAD)
		self.assertIn("h2", tracker.dead_at)
		self.assertNotIn("h2", tracker.leaving_at)  # countdown consumed

	def test_leaving_promotion_driven_by_loop_gc(self):
		# End-to-end via the loop's `_gc_if_due`: a leaving record → dead after
		# leaving_grace, membership reaped after dead_grace (§14.4 hands off to
		# the normal ladder). Uses default timers.
		from atlas.networkd.loop import Loop

		daemon, tracker, clock = self._daemon_with_tracker()
		daemon.state.apply_membership(member("h2", 1))
		msg = Message(type="gossip", sender="h2", payload=wire.gossip_payload([leaving_member("h2", 2)]))
		handle_message(msg, ("2001:db9::h2", 7946), daemon, GossipState())
		loop = Loop(daemon=daemon, tick_interval=0.001, now_fn=lambda: clock[0])
		# Past leaving_grace (2 s) but before dead_grace (30 s): dead, still in
		# membership.
		clock[0] = 3.0
		loop._gc_if_due(clock[0])
		self.assertEqual(tracker.state_of("h2"), FailureState.DEAD)
		self.assertIn("h2", daemon.state.membership)
		# Past dead_grace: membership reaped (h2 owns nothing → no routable_dead).
		clock[0] = 35.0
		loop._gc_if_due(clock[0])
		self.assertNotIn("h2", daemon.state.membership)

	# (c) a leaving host is NOT selected as a probe/gossip/anti-entropy target.
	def test_leaving_host_excluded_from_target_selection(self):
		from atlas.networkd.peers import select_peers

		daemon, _tracker, _clock = self._daemon_with_tracker()
		daemon.state.apply_membership(member("h2", 1))
		daemon.state.apply_membership(leaving_member("h3", 2))  # h3 is leaving
		# Gossip / anti-entropy target selection (wire-state filter drops leaving).
		peers = select_peers(daemon.state.membership, "h1", count=5)
		self.assertIn("h2", peers)
		self.assertNotIn("h3", peers)
		# Probe selection (probe.py builds `eligible` then calls select_peers).
		daemon.failure_tracker = _tracker
		probe = ProbeProtocol(tracker=_tracker, config=daemon.config, now_fn=lambda: 0.0)
		eligible = {
			h: m
			for h, m in daemon.state.membership.items()
			if h != "h1" and probe.tracker.state_of(h).value == "alive"
		}
		probe_targets = select_peers(eligible, "h1", count=5)
		self.assertNotIn("h3", probe_targets)

	# (d) an alive refute at a higher generation BEFORE leaving_grace elapses
	# CANCELS the countdown (host stays alive, not reaped).
	def test_alive_refute_before_grace_cancels_countdown(self):
		daemon, tracker, clock = self._daemon_with_tracker()
		daemon.state.apply_membership(member("h2", 1))
		# h2 announces leaving at gen 2.
		msg = Message(type="gossip", sender="h2", payload=wire.gossip_payload([leaving_member("h2", 2)]))
		handle_message(msg, ("2001:db9::h2", 7946), daemon, GossipState())
		self.assertIn("h2", tracker.leaving_at)
		# Sub-leaving_grace `systemctl restart`: h2 re-advertises alive at gen 3.
		clock[0] = 1.0  # < leaving_grace (2 s)
		refute = Message(type="gossip", sender="h2", payload=wire.gossip_payload([member("h2", 3)]))
		handle_message(refute, ("2001:db9::h2", 7946), daemon, GossipState())
		# The countdown is cancelled; the host stays alive.
		self.assertNotIn("h2", tracker.leaving_at)
		self.assertEqual(tracker.state_of("h2"), FailureState.ALIVE)
		# And the grace elapsing now reaps nothing.
		clock[0] = 5.0
		self.assertEqual(tracker.promote_leaving_if_due(daemon.config.leaving_grace), [])
		self.assertEqual(tracker.state_of("h2"), FailureState.ALIVE)

	def test_note_alive_clears_leaving_at_directly(self):
		# Tracker-level: note_alive must clear a pending leaving_at (§14.4 step 3).
		clock = [0.0]
		t = FailureTracker(now_fn=lambda: clock[0])
		t.note_leaving("h2")
		self.assertIn("h2", t.leaving_at)
		t.note_alive("h2")
		self.assertNotIn("h2", t.leaving_at)
		self.assertEqual(t.state_of("h2"), FailureState.ALIVE)

	def test_note_leaving_keeps_original_timestamp(self):
		# Re-delivery of the leaving record (or a later one) doesn't reset the
		# grace clock — it runs from the FIRST notice.
		clock = [0.0]
		t = FailureTracker(now_fn=lambda: clock[0])
		t.note_leaving("h2")
		clock[0] = 1.5
		t.note_leaving("h2")  # second delivery
		self.assertEqual(t.leaving_at["h2"], 0.0)  # original, not 1.5

	# (e) a leaving host that owns /128s still gets the H7 `routable_dead` grace
	# once it goes dead (H6 must not regress H7).
	def test_leaving_owner_keeps_routable_dead_grace(self):
		from atlas.networkd.loop import Loop

		daemon, tracker, clock = self._daemon_with_tracker()
		owned = "fdaa:1::99"
		daemon.state.apply_membership(member("h2", 1))
		daemon.state.apply_ownership(ownership("h2", 1, owned))
		# h2 leaves at gen 2.
		msg = Message(type="gossip", sender="h2", payload=wire.gossip_payload([leaving_member("h2", 2)]))
		handle_message(msg, ("2001:db9::h2", 7946), daemon, GossipState())
		loop = Loop(daemon=daemon, tick_interval=0.001, now_fn=lambda: clock[0])
		# leaving_grace (2) → dead at t=3; dead_grace (30) reaps membership at
		# t=35 but h2 OWNS a /128, so it goes to routable_dead (H7), not gone.
		clock[0] = 3.0
		loop._gc_if_due(clock[0])
		self.assertEqual(tracker.state_of("h2"), FailureState.DEAD)
		dead_at = tracker.dead_at["h2"]
		clock[0] = 35.0
		loop._gc_if_due(clock[0])
		self.assertNotIn("h2", daemon.state.membership)  # membership reaped
		self.assertIn("h2", daemon.state.routable_dead)  # H7: kept render-only
		self.assertIn("h2", daemon.state.ownership)  # routes survive
		out = daemon.render_current()
		self.assertIn(f"{owned}/128", out)  # still routes during ownership_grace
		# ownership_grace measured from dead_at (t=3): reaped past t=63.
		self.assertLess(clock[0] - dead_at, daemon.config.ownership_grace)


if __name__ == "__main__":
	unittest.main()
