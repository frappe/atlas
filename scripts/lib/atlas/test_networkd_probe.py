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


if __name__ == "__main__":
	unittest.main()
