"""Unit tests for Stage 3 — anti-entropy (spec §15).

The headline `TestAntiEntropyEndToEnd.test_partition_heals_via_anti_entropy`
runs the full protocol in-memory: two `Daemon`s on the same bus; A has records
B doesn't (simulating a partition just healed, where gossip's piggyback never
	picked them up); A runs `anti_entropy_round`; B drains the REQ, builds the
	RESP with its records; A drains the RESP and applies them.

The naive pull (Stage 3) is correctness-equivalent to the Merkle acceleration
	(§15.3) — Merkle only saves bytes on the HEALTHY-cluster steady state. We
	test the naive path because that's what ships; the same tests cover the
	Merkle path when / if it lands.
"""

import random
import tempfile
import unittest
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from atlas.networkd import wire
from atlas.networkd.antientropy import (
	DEFAULT_ANTIENTROPY_RECORDS_MAX,
	anti_entropy_resp_payload,
	anti_entropy_round,
	build_vector,
	handle_anti_entropy_req,
	handle_anti_entropy_resp,
	parse_anti_entropy_req_payload,
	parse_anti_entropy_resp_payload,
)
from atlas.networkd.config import Config
from atlas.networkd.daemon import Daemon, build_initial
from atlas.networkd.gossip import GossipState, gossip_round, handle_message
from atlas.networkd.identity import HostIdentity
from atlas.networkd.records import (
	MembershipKind,
	MembershipRecord,
	MemberState,
	OwnershipAdvertisement,
	owning_advertisement,
)
from atlas.networkd.state import AppliedState
from atlas.networkd.wire import (
	MAX_DATAGRAM_BYTES,
	TYPE_ANTI_ENTROPY_REQ,
	TYPE_ANTI_ENTROPY_RESP,
	Message,
	from_bytes,
)

# Reuse the helpers from test_networkd_gossip (kept here separately to mirror
# the test-per-module convention; no cross-module test import).


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
	"""A test Daemon with a tmpdir data_dir and every host-touching seam stubbed
	(so build_initial's save_state + the loop's apply don't touch the kernel)."""
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
	"""Same shape as `test_networkd_gossip.FakeTransport` (inlined here so this
	module is standalone — the two-host end-to-end tests want it)."""

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


# --- build_vector ------------------------------------------------------------


class TestBuildVector(unittest.TestCase):
	def test_empty_state_yields_empty_vector(self):
		daemon = _daemon("h1")
		v = build_vector(daemon.state)
		# `build_initial` already applied h1's own Membership Record at gen 1,
		# so the vector isn't literally empty — it carries the daemon's own
		# origin. Ownership is empty (no scan ran yet in the test).
		self.assertEqual(v, {"vector_m": {"h1": 1}, "vector_o": {}})

	def test_vector_carries_latest_gen_per_origin(self):
		daemon = _daemon("h1")
		daemon.state.apply_membership(member("h2", 5, key="K"))
		daemon.state.apply_membership(member("h3", 7, key="K2"))
		daemon.state.apply_ownership(ownership("h2", 3, "fdaa::1"))
		v = build_vector(daemon.state)
		self.assertEqual(v["vector_m"], {"h1": 1, "h2": 5, "h3": 7})
		self.assertEqual(v["vector_o"], {"h2": 3})


# --- request payload (de)serialization --------------------------------------


class TestReqPayload(unittest.TestCase):
	def test_round_trip(self):
		v = {"vector_m": {"h1": 3, "h2": 5}, "vector_o": {"h1": 1}}
		parsed = parse_anti_entropy_req_payload(anti_entropy_req_payload(v))
		self.assertEqual(parsed, v)

	def test_missing_vectors_default_empty(self):
		parsed = parse_anti_entropy_req_payload({})
		self.assertEqual(parsed, {"vector_m": {}, "vector_o": {}})

	def test_malformed_payload_raises(self):
		with self.assertRaises(ValueError):
			parse_anti_entropy_req_payload([])  # not a dict


def anti_entropy_req_payload(vector):
	"""Inline (kept in the test file so the antientropy module's `__all__`
	stays small — this is the trivial wrapper around the vector)."""
	return vector


# --- response payload (de)serialization -------------------------------------


class TestRespPayload(unittest.TestCase):
	def test_round_trip_with_records(self):
		records = [member("h2", 5), ownership("h2", 1, "fdaa::9")]
		payload = anti_entropy_resp_payload(records, {"m": {"h3": 2}, "o": {}})
		parsed_records, newer = parse_anti_entropy_resp_payload(payload)
		self.assertEqual(len(parsed_records), 2)
		# Records come back as tagged dicts, decoded via wire.decode_record.
		decoded = [wire.decode_record(t) for t in parsed_records]
		self.assertEqual(decoded, records)
		self.assertEqual(newer, {"m": {"h3": 2}, "o": {}})

	def test_empty_payload_decodes_to_empty(self):
		records, newer = parse_anti_entropy_resp_payload({})
		self.assertEqual(records, [])
		self.assertEqual(newer, {})


# --- _missing_for_requester (the response-builder core) ---------------------


class TestMissingForRequester(unittest.TestCase):
	def test_responder_owes_records_requester_lacks(self):
		daemon = _daemon("h1")
		daemon.state.apply_membership(member("h2", 5))
		daemon.state.apply_ownership(ownership("h2", 3, "fdaa::1"))
		# Requester's vector is empty — it's never heard of h2. We owe our own
		# h1 MembershipRecord (from build_initial), h2, and the ownership adv.
		from atlas.networkd.antientropy import _missing_for_requester

		records, newer = _missing_for_requester(daemon.state, {"vector_m": {}, "vector_o": {}})
		self.assertEqual(len(records), 3)  # h1 (our own) + h2 + the ownership record
		self.assertEqual(newer, {"m": {}, "o": {}})

	def test_responder_owes_nothing_when_requester_up_to_date(self):
		daemon = _daemon("h1")
		daemon.state.apply_membership(member("h2", 5))
		# Requester has gen 5 too — equal; nothing owed. (Includes h1.)
		from atlas.networkd.antientropy import _missing_for_requester

		records, newer = _missing_for_requester(
			daemon.state, {"vector_m": {"h1": 1, "h2": 5}, "vector_o": {}}
		)
		self.assertEqual(records, [])
		self.assertEqual(newer, {"m": {}, "o": {}})

	def test_responder_owes_record_when_requester_stale(self):
		daemon = _daemon("h1")
		daemon.state.apply_membership(member("h2", 7))
		# Requester has h2 at gen 5 — we have gen 7; we owe gen 7. (It also has
		# h1 at gen 1, so our own record isn't owed.)
		from atlas.networkd.antientropy import _missing_for_requester

		records, newer = _missing_for_requester(
			daemon.state, {"vector_m": {"h1": 1, "h2": 5}, "vector_o": {}}
		)
		self.assertEqual(len(records), 1)
		self.assertEqual(records[0].generation, 7)
		self.assertEqual(newer, {"m": {}, "o": {}})

	def test_responder_asks_back_when_requester_ahead(self):
		# Mutual healing: requester has a higher gen than us for some origin →
		# we add to `newer_on_initiator` so it can reverse-push to us.
		daemon = _daemon("h1")
		daemon.state.apply_membership(member("h2", 5))
		# Requester claims gen 9 for h2 (we have gen 5). h1's own gen claim
		# matches ours → not owed, not asked back.
		from atlas.networkd.antientropy import _missing_for_requester

		records, newer = _missing_for_requester(
			daemon.state, {"vector_m": {"h1": 1, "h2": 9}, "vector_o": {}}
		)
		self.assertEqual(records, [])
		self.assertEqual(newer, {"m": {"h2": 9}, "o": {}})

	def test_unknown_origin_in_requester_vector_asked_back(self):
		# A requester who's heard of h3 (we haven't) → ask them to push it.
		# Our own h1 record isn't owed (requester vector includes h1).
		daemon = _daemon("h1")
		from atlas.networkd.antientropy import _missing_for_requester

		records, newer = _missing_for_requester(
			daemon.state, {"vector_m": {"h1": 1, "h3": 4}, "vector_o": {}}
		)
		self.assertEqual(records, [])
		self.assertEqual(newer, {"m": {"h3": 4}, "o": {}})


# --- anti_entropy_round (the pull request) ----------------------------------


class TestAntiEntropyRound(unittest.TestCase):
	def test_no_peers_no_send(self):
		daemon = _daemon("h1")
		bus = Bus()
		t = FakeTransport(bind=("2001:db9::h1", 7946), bus=bus)
		daemon.transport = t
		sent = anti_entropy_round(daemon, t, rng=random.Random(0))
		self.assertEqual(sent, 0)

	def test_sends_req_to_selected_peer(self):
		daemon = _daemon("h1")
		daemon.state.apply_membership(member("h2", 1))
		daemon.state.apply_membership(member("h3", 1))
		bus = Bus()
		t = FakeTransport(bind=("2001:db9::h1", 7946), bus=bus)
		daemon.transport = t
		sent = anti_entropy_round(daemon, t, rng=random.Random(0))  # determinism via seeded RNG
		self.assertEqual(sent, 1)
		# Exactly one peer got a REQ.
		total_reqs = sum(
			1
			for queue in bus.queues.values()
			for data, _ in queue
			if from_bytes(data).type == TYPE_ANTI_ENTROPY_REQ
		)
		self.assertEqual(total_reqs, 1)


# --- handle_anti_entropy_req (the responder) --------------------------------


class TestHandleAntiEntropyReq(unittest.TestCase):
	def test_responder_replies_with_records_requester_lacks(self):
		# h2 has h3's records; h1 sends a REQ with an empty vector → h2 replies
		# with every record h2 has that h1 lacks.
		h1 = _daemon("h1")
		h2 = _daemon("h2")
		h2.state.apply_membership(member("h3", 7, key="K-H3"))
		h2.state.apply_ownership(ownership("h3", 1, "fdaa::3"))
		# h2 needs to know h1's mesh_address to reply — so apply h1's record.
		h2.state.apply_membership(h1.own_membership)
		bus = Bus()
		h1_t = FakeTransport(bind=("2001:db9::h1", 7946), bus=bus)
		h2_t = FakeTransport(bind=("2001:db9::h2", 7946), bus=bus)
		h1.transport = h1_t
		h2.transport = h2_t
		req = Message(type=TYPE_ANTI_ENTROPY_REQ, sender="h1", payload={"vector_m": {}, "vector_o": {}})
		handle_anti_entropy_req(req, h2, GossipState())
		# h1's queue should have exactly one RESP.
		data, _ = bus.queues[("2001:db9::h1", 7946)].pop()
		msg = from_bytes(data)
		self.assertEqual(msg.type, TYPE_ANTI_ENTROPY_RESP)
		records_tagged, newer = parse_anti_entropy_resp_payload(msg.payload)
		decoded = {wire.decode_record(t) for t in records_tagged}
		# The reply contains h3's Membership Record (h2 has it; h1 doesn't)
		# and h3's Ownership Advertisement.
		has_h3_membership = any(isinstance(r, MembershipRecord) and r.host_id == "h3" for r in decoded)
		has_h3_ownership = any(isinstance(r, OwnershipAdvertisement) and r.origin == "h3" for r in decoded)
		self.assertTrue(has_h3_membership)
		self.assertTrue(has_h3_ownership)
		self.assertEqual(newer, {"m": {}, "o": {}})

	def test_unknown_requester_dropped(self):
		# A REQ from a host the responder has never heard of (no mesh_address)
		# is dropped quietly — the requester will retry after their cold-join
		# Membership Advertisement has propagated.
		h2 = _daemon("h2")
		bus = Bus()
		h2_t = FakeTransport(bind=("2001:db9::h2", 7946), bus=bus)
		h2.transport = h2_t
		req = Message(
			type=TYPE_ANTI_ENTROPY_REQ, sender="unknown-host", payload={"vector_m": {}, "vector_o": {}}
		)
		handle_anti_entropy_req(req, h2, GossipState())
		# Nothing got sent.
		self.assertEqual(bus.queues, {})


# --- handle_anti_entropy_resp (the requester) -------------------------------


class TestHandleAntiEntropyResp(unittest.TestCase):
	def test_requester_applies_records_from_response(self):
		# h1 (requester) is missing h3's records; h2 (responder) shipped them.
		h1 = _daemon("h1")
		h2 = _daemon("h2")
		h1.state.apply_membership(h2.own_membership)  # h1 knows h2 (so recv is valid)
		records = [member("h3", 7, key="K-H3"), ownership("h3", 1, "fdaa::3")]
		resp = Message(
			type=TYPE_ANTI_ENTROPY_RESP,
			sender="h2",
			payload=anti_entropy_resp_payload(records, {"m": {}, "o": {}}),
		)
		gossip_state = GossipState()
		freshly = handle_anti_entropy_resp(resp, h1, gossip_state)
		# Both records were freshly applied (h1 had neither before).
		self.assertEqual(len(freshly), 2)
		self.assertIn("h3", h1.state.membership)
		self.assertEqual(h1.state.membership["h3"].generation, 7)
		self.assertIn("h3", h1.state.ownership)

	def test_stale_record_in_response_dropped(self):
		# A response that carries a record below the requester's current gen
		# for that origin: applied via the §13.2 monotonic rule, drops silently.
		h1 = _daemon("h1")
		h1.state.apply_membership(member("h3", 10))  # h1 has h3 at gen 10 already
		resp = Message(
			type=TYPE_ANTI_ENTROPY_RESP,
			sender="h2",
			payload=anti_entropy_resp_payload([member("h3", 3)], {"m": {}, "o": {}}),
		)
		freshly = handle_anti_entropy_resp(resp, h1, GossipState())
		self.assertEqual(freshly, [])  # the stale record was NOT freshly applied
		self.assertEqual(h1.state.membership["h3"].generation, 10)  # unchanged

	def test_mutual_healing_reverse_pushes(self):
		# h1 (requester) sends REQ to h2; h2 responds with newer_on_initiator
		# flagging h1 SHOULD push h3's records back to h2. h1's response handler
		# reverse-pushes a Gossip carrying h3 (h1 has h3 at gen ≥ the response).
		# We assert the reverse-push Gossip datagram lands on h2's queue.
		h1 = _daemon("h1")
		h2 = _daemon("h2")
		# h1 has h3 (h2 doesn't); h2's request would have flagged h3 in
		# `newer_on_initiator`. Simulate the response that comes back from h2.
		h1.state.apply_membership(h2.own_membership)  # h1 knows h2's mesh_address
		h1.state.apply_membership(member("h3", 7, key="K-H3"))
		bus = Bus()
		h1_t = FakeTransport(bind=("2001:db9::h1", 7946), bus=bus)
		h2_t = FakeTransport(bind=("2001:db9::h2", 7946), bus=bus)
		h1.transport = h1_t
		h2.transport = h2_t
		# h2's RESP tells h1 "I'm missing h3" — h1 reverse-pushes h3 to h2.
		resp = Message(
			type=TYPE_ANTI_ENTROPY_RESP,
			sender="h2",
			payload=anti_entropy_resp_payload([], {"m": {"h3": 0}, "o": {}}),
		)
		handle_anti_entropy_resp(resp, h1, GossipState())
		# h2's queue has a Gossip carrying h3's Membership Record.
		data, _ = bus.queues[("2001:db9::h2", 7946)].pop()
		msg = from_bytes(data)
		from atlas.networkd.wire import TYPE_GOSSIP, parse_gossip_payload

		self.assertEqual(msg.type, TYPE_GOSSIP)
		records = parse_gossip_payload(msg.payload)
		self.assertTrue(any(isinstance(r, MembershipRecord) and r.host_id == "h3" for r in records))

	def test_response_records_fit_in_datagram(self):
		# A response carrying many records trims to fit MAX_DATAGRAM_BYTES. The
		# trim logic lives in `_serialize_with_trim` (which mutates the payload
		# in place) — `to_bytes` itself raises if over the cap. We assert the
		# trim helper produces a fit.
		from atlas.networkd.antientropy import _serialize_with_trim

		h1 = _daemon("h1")
		h2 = _daemon("h2")
		h1.state.apply_membership(h2.own_membership)
		records = [ownership(f"h{i}", 1, *[f"fdaa:1::{j}" for j in range(5)]) for i in range(200)]
		resp = Message(
			type=TYPE_ANTI_ENTROPY_RESP,
			sender="h2",
			payload=anti_entropy_resp_payload(records, {"m": {}, "o": {}}),
		)
		raw = _serialize_with_trim(resp)
		self.assertLessEqual(len(raw), MAX_DATAGRAM_BYTES)


# --- end-to-end: two daemons converge via anti-entropy alone ----------------


class TestAntiEntropyEndToEnd(unittest.TestCase):
	def test_partition_heals_via_anti_entropy(self):
		# Two hosts on the same bus. A has h3's records (gossip wave dropped);
		# B has nothing besides A and itself. Before anti-entropy: B doesn't
		# know h3. After A → B REQ/RESP: B has h3's records at the right gens.
		a = _daemon("ha")
		b = _daemon("hb")
		a.state.apply_membership(b.own_membership)
		b.state.apply_membership(a.own_membership)
		# A applies h3's records at gen 7 / gen 1 (h3 isn't actually live —
		# simulate the records arrived via a gossip piggyback that B missed).
		a.state.apply_membership(member("h3", 7, key="K-H3"))
		a.state.apply_ownership(ownership("h3", 1, "fdaa::3"))
		# B knows h3's mesh_address so an eventual reverse-push could close the
		# gap, but here we only need A → B to heal B's missing h3 record.
		b.state.apply_membership(member("h3", 1, key="K-H3-PHANTOM"))  # B "knows" h3 at gen 1
		bus = Bus()
		a_t = FakeTransport(bind=("2001:db9::ha", 7946), bus=bus)
		b_t = FakeTransport(bind=("2001:db9::hb", 7946), bus=bus)
		a.transport = a_t
		b.transport = b_t
		# A runs ONE anti-entropy round → REQ to B.
		anti_entropy_round(a, a_t, rng=random.Random(0))
		# B drains, builds the RESP. (B doesn't owe A anything; A just wanted
		# the comparison.) Actually A doesn't owe the RESP — A sent the REQ; B
		# is the responder. B's vector lacks h3 too (B has h3 at gen 1, A has 7;
		# so B's RESP carries h3 at gen 7 — which A submitted to B as part of
		# A's response! No: A is the requester; B is the responder.)
		# Let me re-verify: A sent the REQ to B. B's `handle_anti_entropy_req`
		# compares A's vector (which carries A's gen for h3 = 7) to B's state
		# (B has h3 at gen 1). B sees A is AHEAD; so B's `newer_on_initiator`
		# flags h3, AND B doesn't owe A any records. So B's RESP carries no
		# records but DOES flag h3 for reverse-push. A then reverse-pushes h3
		# at gen 7 to B. B drains and applies → B's h3 record advances to 7.
		b_gs = GossipState()
		b_t.drain(lambda msg, addr: handle_message(msg, addr, b, b_gs))
		# A drains the RESP (which carries no records; just the newer vector).
		a_gs = GossipState()
		a_t.drain(lambda msg, addr: handle_message(msg, addr, a, a_gs))
		# A's `_reverse_push` sent h3's latest gen-7 record to B as a Gossip.
		# Drain it on B and assert B's h3 gen is now 7.
		b_t.drain(lambda msg, addr: handle_message(msg, addr, b, b_gs))
		self.assertEqual(b.state.membership["h3"].generation, 7)

	def test_naive_pull_pairs_with_gossip_for_partition_recover(self):
		# Sanity: anti-entropy and gossip can both flow, and a record A holds
		# at a higher gen than B's gossip piggybacked version flows via the
		# RESP to overwrite B's stale copy. This requires THREE drains:
		#   (1) B drains A's REQ → B sends RESP (no records; A is ahead; the
		#       RESP carries h3 in `newer_on_initiator`).
		#   (2) A drains B's RESP → A reverse-pushes h3 at gen 9 to B as a
		#       Gossip unicast.
		#   (3) B drains the reverse-push Gossip → B applies h3 at gen 9.
		a = _daemon("ha")
		b = _daemon("hb")
		a.state.apply_membership(b.own_membership)
		b.state.apply_membership(a.own_membership)
		a.state.apply_membership(member("h3", 9, key="K-H3"))
		b.state.apply_membership(member("h3", 3, key="K-H3-OLD"))
		bus = Bus()
		a_t = FakeTransport(bind=("2001:db9::ha", 7946), bus=bus)
		b_t = FakeTransport(bind=("2001:db9::hb", 7946), bus=bus)
		a.transport = a_t
		b.transport = b_t
		# A → B pull (A sends REQ carrying A's vector — h3 at gen 9).
		anti_entropy_round(a, a_t, rng=random.Random(0))
		# Step 1: B drains REQ, sends RESP.
		b_gs = GossipState()
		b_t.drain(lambda msg, addr: handle_message(msg, addr, b, b_gs))
		# Step 2: A drains RESP, reverse-pushes a Gossip with h3 at gen 9.
		a_gs = GossipState()
		a_t.drain(lambda msg, addr: handle_message(msg, addr, a, a_gs))
		# Step 3: B drains the reverse-push Gossip.
		b_t.drain(lambda msg, addr: handle_message(msg, addr, b, b_gs))
		self.assertEqual(b.state.membership["h3"].generation, 9)
		self.assertEqual(b.state.membership["h3"].wg_public_key, "K-H3")


if __name__ == "__main__":
	unittest.main()
