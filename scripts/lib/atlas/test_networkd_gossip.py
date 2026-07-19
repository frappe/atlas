"""Unit tests for Stage 2 — wire format, peer selection, gossip fan-out + apply,
and the §9.1 cold-join unicast + bundle reply. Run with bare `python3 -m unittest`
— every host-touching seam is injected, just like in Stage 1's tests.

The headline test (`TestGossipEndToEnd.test_two_daemons_converge`) runs the
full protocol in-memory: two `Daemon`s wired to in-memory `FakeTransport`s
connected by a queue; one rounds of gossip propagates a Membership update + an
Ownership update from A to B; B's state now has A's records at the right
generations.
"""

import json
import random
import unittest
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from atlas.networkd import wire
from atlas.networkd.config import Config
from atlas.networkd.daemon import Daemon, build_initial
from atlas.networkd.gossip import (
	DEFAULT_PIGGYBACK_MAX,
	GossipState,
	gossip_round,
	handle_message,
)
from atlas.networkd.identity import HostIdentity
from atlas.networkd.join import cold_join
from atlas.networkd.peers import select_peers
from atlas.networkd.records import (
	MembershipKind,
	MembershipRecord,
	MemberState,
	owning_advertisement,
)
from atlas.networkd.state import AppliedState
from atlas.networkd.transport import UdpTransport
from atlas.networkd.wire import (
	MAX_DATAGRAM_BYTES,
	TYPE_GOSSIP,
	TYPE_MEMBERSHIP_ADVERT,
	DatagramTooLarge,
	Message,
	from_bytes,
)

# --- helpers -----------------------------------------------------------------


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


def _daemon(host_id: str, gen: int = 1, owned: frozenset[str] | None = None) -> Daemon:
	"""A Daemon wired for tests: every host-touching seam replaced with a stub,
	the transport initialized to None (the test injects a FakeTransport)."""
	import tempfile

	data_dir = tempfile.mkdtemp(prefix="atlas-networkd-test-")
	cfg = Config().with_overrides(
		gossip_interval=0.001,
		gossip_fanout=3,
		apply_debounce=0.001,
		ownership_scan_interval=10.0,
		data_dir=data_dir,
	)
	ident = HostIdentity(
		host_id=host_id,
		endpoint=f"2001:db9::{host_id}",
		mesh_address=f"fdaa:0:0:{host_id}::1",
	)
	state = AppliedState()
	pubkey = f"PUB-{host_id}"
	daemon = build_initial(ident, cfg, state, public_key=pubkey)
	# Seed the daemon's own ownership advertisement (build_initial doesn't — a
	# Stage 1b Daemon starts with an empty /128 set; the loop's scan fills it).
	if owned is not None:
		daemon.state.apply_ownership(owning_advertisement(host_id, gen, owned))
		daemon.last_local_set = owned
	# Replace every host-touching seam with a no-op so render/apply don't run
	# subprocess / write files.
	daemon.run = lambda *a, **kw: ""  # type: ignore[method-assign]
	daemon.write_run_config = lambda body: None  # type: ignore[method-assign]
	return daemon


@dataclass(slots=True)
class FakeTransport:
	"""A pair-able UDP stand-in: `send` enqueues onto a shared bus keyed by
	target mesh_address; `drain` pulls any datagrams addressed to MY bind
	address. Two `FakeTransport`s constructed against the same `Bus` form the
	two-way link tests use — no kernel socket, no port collision, fully
	synchronous under the test's control."""

	bind: tuple[str, int]
	bus: "Bus" = field(default_factory=lambda: Bus())
	socket: object | None = field(default=object())  # truthy so 'cold_join' is happy

	def start(self) -> None:
		"""No-op — FakeTransport has no kernel socket."""
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
	"""A dict-of-queues — the shared in-memory link between two FakeTransports.
	`_send` enqueues; `_recv` dequeues. Per-bind FIFO so order is preserved
	within one tick."""

	queues: dict[tuple[str, int], list[tuple[bytes, tuple[str, int]]]] = field(
		default_factory=lambda: defaultdict(list)
	)

	def _send(self, _source: tuple[str, int], target: tuple[str, int], data: bytes) -> None:
		self.queues[target].append((data, _source))

	def _recv(self, me: tuple[str, int]) -> tuple[bytes, tuple[str, int]] | None:
		q = self.queues.get(me)
		if not q:
			return None
		return q.pop(0)


# --- wire --------------------------------------------------------------------


class TestWire(unittest.TestCase):
	def test_message_round_trip(self):
		m = Message(type=TYPE_GOSSIP, sender="h1", payload=[])
		data = m.to_bytes()
		round_tripped = from_bytes(data)
		self.assertEqual(round_tripped.type, TYPE_GOSSIP)
		self.assertEqual(round_tripped.sender, "h1")
		self.assertEqual(round_tripped.payload, [])

	def test_message_rejects_missing_type(self):
		bad = json.dumps({"sender": "h1", "payload": []}).encode("utf-8")
		with self.assertRaises(ValueError):
			from_bytes(bad)

	def test_membership_to_dict_round_trip(self):
		m = member("h1", 7, key="K")
		d = wire.membership_to_dict(m)
		self.assertEqual(wire.membership_from_dict(d), m)

	def test_ownership_to_dict_round_trip_canonical_sorted(self):
		a = ownership("h1", 3, "fdaa::2", "fdaa::1")
		d = wire.ownership_to_dict(a)
		# owned is sorted so the bytes are canonical regardless of input order.
		self.assertEqual(d["owned"], ["fdaa::1", "fdaa::2"])
		self.assertEqual(wire.ownership_from_dict(d), a)

	def test_max_datagram_enforced(self):
		# A 1281-byte payload must raise DatagramTooLarge on to_bytes().
		big = Message(type=TYPE_GOSSIP, sender="h1", payload=["x" * (MAX_DATAGRAM_BYTES + 100)])
		with self.assertRaises(DatagramTooLarge):
			big.to_bytes()


# --- peer selection ----------------------------------------------------------


class TestSelectPeers(unittest.TestCase):
	def test_excludes_self(self):
		m = {h: member(h, 1) for h in ("h1", "h2", "h3")}
		picked = select_peers(m, "h1", count=2, rng=random.Random(0))
		self.assertNotIn("h1", picked)
		self.assertEqual(len(picked), 2)

	def test_empty_when_only_self(self):
		m = {"h1": member("h1", 1)}
		self.assertEqual(select_peers(m, "h1", count=3, rng=random.Random(0)), [])

	def test_count_capped_at_pool(self):
		m = {h: member(h, 1) for h in ("h2", "h3")}
		# Asking for 5 peers but only 2 available — returns 2, not crash.
		self.assertEqual(len(select_peers(m, "h1", count=5, rng=random.Random(0))), 2)

	def test_zero_count_returns_empty(self):
		m = {h: member(h, 1) for h in ("h2", "h3")}
		self.assertEqual(select_peers(m, "h1", count=0, rng=random.Random(0)), [])


# --- gossip: handle_message / round -----------------------------------------


class TestHandleGossip(unittest.TestCase):
	def test_apply_membership_via_gossip(self):
		daemon = _daemon("h1", owned=frozenset())
		gossip_state = GossipState()
		# A peer sends us its Membership Record at gen 5.
		peer = member("h2", 5, key="K-H2")
		msg = Message(
			type=TYPE_GOSSIP,
			sender="h2",
			payload=wire.gossip_payload([peer]),
		)
		handle_message(msg, ("2001:db9::h2", 7946), daemon, gossip_state)
		self.assertIn("h2", daemon.state.membership)
		self.assertEqual(daemon.state.membership["h2"].generation, 5)
		# Freshly-applied records enter the forward queue (we owe them).
		self.assertEqual(len(gossip_state.forward_queue), 1)

	def test_stale_generation_dropped_not_forwarded(self):
		# A lower-gen from the same origin: dropped (Issue C), and NOT
		# re-forwarded (we don't propagate stale state).
		daemon = _daemon("h1", owned=frozenset())
		daemon.state.apply_membership(member("h2", 10, key="K-H2"))
		gossip_state = GossipState()
		stale = member("h2", 3, key="K-H2-OLD")
		msg = Message(type=TYPE_GOSSIP, sender="h2", payload=wire.gossip_payload([stale]))
		handle_message(msg, ("2001:db9::h2", 7946), daemon, gossip_state)
		# The membership table still has gen-10, NOT the gen-3 stale.
		self.assertEqual(daemon.state.membership["h2"].generation, 10)
		# Nothing was added to the forward queue.
		self.assertEqual(gossip_state.forward_queue, [])

	def test_apply_ownership_via_gossip(self):
		daemon = _daemon("h1", owned=frozenset({"fdaa::1"}))
		gossip_state = GossipState()
		# Peer h2 advertises ownership of fdaa::9 at gen 1.
		adv = ownership("h2", 1, "fdaa::9")
		msg = Message(
			type=TYPE_GOSSIP,
			sender="h2",
			payload=wire.gossip_payload([adv]),
		)
		handle_message(msg, ("2001:db9::h2", 7946), daemon, gossip_state)
		# h2's advertisement is in the table at gen 1, advertising fdaa::9.
		self.assertEqual(daemon.state.ownership["h2"].owned, frozenset({"fdaa::9"}))

	def test_mixed_piggyback(self):
		# A Gossip carries both Membership + Ownership records; the receiver
		# applies each via its own rule.
		daemon = _daemon("h1", owned=frozenset())
		gossip_state = GossipState()
		peer_m = member("h2", 1, key="K-H2")
		peer_o = ownership("h2", 1, "fdaa::7")
		msg = Message(
			type=TYPE_GOSSIP,
			sender="h2",
			payload=wire.gossip_payload([peer_m, peer_o]),
		)
		handle_message(msg, ("2001:db9::h2", 7946), daemon, gossip_state)
		self.assertIn("h2", daemon.state.membership)
		self.assertEqual(daemon.state.ownership["h2"].owned, frozenset({"fdaa::7"}))


# --- gossip_round -----------------------------------------------------------


class TestGossipRound(unittest.TestCase):
	def test_no_peers_returns_zero(self):
		# A lone host with only its own records doesn't gossip.
		daemon = _daemon("h1", owned=frozenset({"fdaa::1"}))
		bus = Bus()
		t = FakeTransport(bind=("2001:db9::h1", 7946), bus=bus)
		daemon.transport = t
		gossip_state = GossipState()
		self.assertEqual(gossip_round(daemon, t, gossip_state), 0)

	def test_fanout_sends_to_selected_peers(self):
		# Host h1 has h2 and h3 as peers — gossip_round should send to each.
		daemon = _daemon("h1", owned=frozenset({"fdaa::1"}))
		daemon.state.apply_membership(member("h2", 1, key="K-H2"))
		daemon.state.apply_membership(member("h3", 1, key="K-H3"))
		bus = Bus()
		t = FakeTransport(bind=("2001:db9::h1", 7946), bus=bus)
		daemon.transport = t
		gossip_state = GossipState()
		# Mark our own records as freshly-applied so they piggyback.
		gossip_state.note_applied(daemon.own_membership)
		gossip_state.note_applied(daemon.state.ownership["h1"])
		sent = gossip_round(daemon, t, gossip_state, select_fn=lambda m, s, c: [h for h in m if h != s][:c])
		self.assertEqual(sent, 2)
		# h2 and h3 each got one datagram on the bus.
		self.assertEqual(len(bus.queues[("2001:db9::h2", 7946)]), 1)
		self.assertEqual(len(bus.queues[("2001:db9::h3", 7946)]), 1)

	def test_datagram_trim_to_fit(self):
		# A piggyback that exceeds MAX_DATAGRAM_BYTES is trimmed by dropping the
		# tail until it fits — the receiver still gets the freshest records.
		daemon = _daemon("h1", owned=frozenset({"fdaa::1"}))
		daemon.state.apply_membership(member("h2", 1, key="K-H2"))
		daemon.state.apply_membership(member("h3", 1, key="K-H3"))
		bus = Bus()
		t = FakeTransport(bind=("2001:db9::h1", 7946), bus=bus)
		daemon.transport = t
		gossip_state = GossipState()
		# Stuff the piggyback with enough records to overflow a 1280-byte
		# datagram — large ownership advertisements from many origins.
		for i in range(50):
			host = f"hx{i}"
			adv = ownership(host, 1, *[f"fdaa:1::{j}" for j in range(20)])
			gossip_state.note_applied(adv)
		sent = gossip_round(daemon, t, gossip_state, select_fn=lambda m, s, c: ["h2"])
		self.assertEqual(sent, 1)
		# The datagram we sent fits under the cap.
		data, _ = bus.queues[("2001:db9::h2", 7946)][0]
		self.assertLessEqual(len(data), MAX_DATAGRAM_BYTES)


# --- cold-join + bundle reply -----------------------------------------------


class TestColdJoin(unittest.TestCase):
	def test_cold_join_sends_membership_advert_to_each_seed(self):
		newcomer = _daemon("n1", owned=frozenset())
		bus = Bus()
		newcomer_t = FakeTransport(bind=("2001:db9::n1", 7946), bus=bus)
		newcomer.transport = newcomer_t
		newcomer_t.socket = object()  # truthy
		seeds = [member("s1", 1, key="K-S1"), member("s2", 1, key="K-S2")]
		sent = cold_join(newcomer, newcomer_t, seeds)
		self.assertEqual(sent, 2)
		# Each seed's queue has one Membership Advertisement datagram.
		for seed in seeds:
			data, _src = bus.queues[(seed.endpoint, 7946)].pop()
			msg = from_bytes(data)
			self.assertEqual(msg.type, TYPE_MEMBERSHIP_ADVERT)
			advertised = wire.parse_membership_advert_payload(msg.payload)
			self.assertEqual(advertised.host_id, "n1")

	def test_seed_bundle_reply_populates_newcomer(self):
		# Full §9.1 sequence: newcomer → seed gets MembershipAdvert; seed
		# replies with bundle; newcomer applies bundle; newcomer now knows
		# every other member the seed knows.
		newcomer = _daemon("n1", owned=frozenset())
		seed = _daemon("s1", owned=frozenset({"fdaa::seed"}))
		# Seed knows about s2, s3 already.
		seed.state.apply_membership(member("s2", 7, key="K-S2"))
		seed.state.apply_membership(member("s3", 3, key="K-S3"))
		bus = Bus()
		newcomer_t = FakeTransport(bind=("2001:db9::n1", 7946), bus=bus)
		seed_t = FakeTransport(bind=("2001:db9::s1", 7946), bus=bus)
		newcomer.transport = newcomer_t
		seed.transport = seed_t

		# Step 1: newcomer → seed advertises itself.
		cold_join(newcomer, newcomer_t, [seed.own_membership])

		# Step 2: seed drains the advert, applies it, sends the bundle reply.
		seed_gs = GossipState()
		seed_t.drain(lambda msg, addr: handle_message(msg, addr, seed, seed_gs))
		self.assertIn("n1", seed.state.membership)  # seed now knows newcomer

		# Step 3: newcomer drains the bundle reply, applies all records.
		newcomer_gs = GossipState()
		newcomer_t.drain(lambda msg, addr: handle_message(msg, addr, newcomer, newcomer_gs))
		# Newcomer now has the seed's record + s2 + s3 (the bundle).
		self.assertIn("s1", newcomer.state.membership)
		self.assertIn("s2", newcomer.state.membership)
		self.assertEqual(newcomer.state.membership["s2"].generation, 7)
		self.assertIn("s3", newcomer.state.membership)
		# And the seed knows the newcomer at newcomer's gen-1 (the cold-join
		# unicast).
		self.assertEqual(seed.state.membership["n1"].generation, 1)


# --- end-to-end: two daemons converge ---------------------------------------


class TestGossipEndToEnd(unittest.TestCase):
	def test_two_daemons_converge(self):
		# Two hosts A and B on the same bus. A gossips; B drains; B has A's
		# records. Then B gossips back; A drains; A has B's records.
		a = _daemon("ha", owned=frozenset({"fdaa:1::1", "fdaa:1::2"}))
		b = _daemon("hb", owned=frozenset({"fdaa:2::1"}))
		# Each knows the other as a peer (seeds installed at bootstrap).
		a.state.apply_membership(b.own_membership)
		b.state.apply_membership(a.own_membership)
		bus = Bus()
		for d in (a, b):
			t = FakeTransport(bind=(	d.identity.endpoint, 7946), bus=bus)
			t.socket = object()
			d.transport = t
		# A's advertising: it has owned /128s the apply_state advertised.
		a_gs = GossipState()
		a_gs.note_applied(a.state.ownership["ha"])
		a_gs.note_applied(a.own_membership)
		gossip_round(a, a.transport, a_gs, select_fn=lambda m, s, c: ["hb"])
		# B drains.
		b_gs = GossipState()
		b.transport.drain(lambda msg, addr: handle_message(msg, addr, b, b_gs))
		# B now has A's ownership advertisement at gen 1.
		self.assertIn("ha", b.state.ownership)
		self.assertEqual(b.state.ownership["ha"].owned, frozenset({"fdaa:1::1", "fdaa:1::2"}))
		# Reverse: B gossips its state back to A; A's table fills with B's ownership.
		b_gs2 = GossipState()
		b_gs2.note_applied(b.state.ownership["hb"])
		gossip_round(b, b.transport, b_gs2, select_fn=lambda m, s, c: ["ha"])
		a_gs_recv = GossipState()
		a.transport.drain(lambda msg, addr: handle_message(msg, addr, a, a_gs_recv))
		self.assertIn("hb", a.state.ownership)
		self.assertEqual(a.state.ownership["hb"].owned, frozenset({"fdaa:2::1"}))


if __name__ == "__main__":
	unittest.main()
