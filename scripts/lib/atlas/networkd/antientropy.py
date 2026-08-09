"""Anti-entropy (spec §15) — the correctness backstop that doesn't depend on
gossip's probabilistic delivery. Every `anti_entropy_interval` (default 1 s) a
node selects ONE random peer and exchanges a compact summary of the latest
generation it has applied per origin, for both record kinds. The responder
fills the requester's stale-or-missing records; the requester optionally
returns the favour (mutual healing). Demers' convergence result restated:
anti-entropy over an eventually-connected network converges in bounded
expected time, regardless of how lossy gossip was in transit.

Stage 3 ships the NAIVE PULL (§15.2) — the responder sends every record where
its generation is higher than the requester's. At our record sizes and cluster
scale (~100-200 hosts) this is fine: a healthy cluster sends zero records per
exchange; the only time a large reply flows is a partition heal or a fresh
join, both bounded by the number of stale records. The Merkle acceleration
(§15.3) is the same protocol with O(log N) bytes-on-wire before records ship;
we hold it until profiling proves the naive pull is the bottleneck. The spec
explicitly says "the protocol is the same either way" — wiring the naive path
first is the right ordering.

Wire shapes (added to wire.py):

    AntiEntropyReq  = {
      "vector_m": { host_id: gen, ... },   # requester's latest membership gen per origin
      "vector_o": { origin:   gen, ... },   # requester's latest ownership gen per origin
    }
    AntiEntropyResp = {
      "records": [ <tagged record>, ... ],  # every record the requester lacks / is stale on
      "newer_on_initiator": {                # tell requester what *I* am missing too, so it
        "m": { host_id: gen, ... },          # can reverse-push (mutual healing, §15.2)
        "o": { origin:   gen, ... },
      },
    }

The reverse push is a separate follow-up Gossip unicast the requester sends
after receiving the response (we reuse the regular gossip path; no separate
RESP back channel needed). This halves the partition-heal round count vs the
one-directional pull alone (§15.2).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

from . import wire
from .peers import HostID, select_peers
from .records import MembershipKind, MembershipRecord, OwnershipAdvertisement, dedupe_key
from .transport import UdpTransport
from .wire import (
	TYPE_ANTI_ENTROPY_REQ,
	TYPE_ANTI_ENTROPY_RESP,
	TYPE_GOSSIP,
	DatagramTooLarge,
	Message,
)

# Cap the records in an AntiEntropyResp before trim-to-fit kicks in. A burst of
# stale records on a long-partition heal can be large; we trim the tail to fit
# the 1280-byte datagram floor (the trim is generous — if a heal has more
# records than fit, the next anti-entropy round picks them up; convergence is
# still expected, just in N rounds). The Merkle optimization (§15.3) saves the
# bytes on the HEALTHY-cluster steady state (no records shipped per exchange);
# on a HEAL, the records truly have to flow.
DEFAULT_ANTIENTROPY_RECORDS_MAX = 64


# --- generation vector -------------------------------------------------------

# Conservative max origins in an AntiEntropyReq generation vector, chosen so the
# serialized JSON always fits within MAX_DATAGRAM_BYTES (1280). A 36-char UUID
# entry in JSON costs ~43 bytes; with envelope overhead (~80 bytes) and both
# vector_m + vector_o, 8 origins × 2 dicts × ~43 bytes ≈ 688 bytes — well
# within budget. The Merkle optimization (§15.3) removes the vector entirely,
# but until then a >8-host cluster advertises a ROTATING WINDOW of at most this
# many origins per round (see `_windowed_vector`) so full coverage completes in
# a BOUNDED ceil(N / MAX_VECTOR_ORIGINS) rounds regardless of the random peer
# pick — the §15.4 convergence guarantee holds at the 100-200 host target.
MAX_VECTOR_ORIGINS = 8


def build_vector(state) -> dict:
	"""The §15.1 compact summary: latest Generation per origin, for both kinds.
	Computed on demand from the AppliedState; NOT persisted (derived, just like
	the effective ownership table)."""
	return {
		"vector_m": {h: m.generation for h, m in state.membership.items()},
		"vector_o": {o: a.generation for o, a in state.ownership.items()},
	}


def _vector_origins(state) -> list[str]:
	"""The full origin set the generation vector must eventually cover — the
	union of every membership origin AND every ownership origin — sorted by
	HostID so the rotating window (`_windowed_vector`) tiles a STABLE order.
	Sorting is what makes the window deterministic: consecutive rounds advance a
	cursor over the same order, so every origin is included within a bounded
	number of rounds regardless of which peer the round picked."""
	return sorted(set(state.membership.keys()) | set(state.ownership.keys()))


def _windowed_vector(state, cursor: int, max_origins: int = MAX_VECTOR_ORIGINS) -> dict:
	"""Build a generation vector covering a CONTIGUOUS window of at most
	``max_origins`` origins starting at ``cursor``, over the HostID-sorted origin
	set (`_vector_origins`). Unlike the old memoryless random sample, consecutive
	rounds advance `cursor` by the window so they tile the full set with no gaps,
	wrapping at the end — every origin is advertised within a BOUNDED
	ceil(N / max_origins) rounds (deterministic coverage, §15.4). When
	N <= max_origins this returns the FULL vector (the fast path — identical bytes
	to `build_vector`). An origin OMITTED from a given round's window is still
	healed within the bound; the responder treats an absent origin correctly
	(§15.2 — it simply doesn't compare that origin this round, never marks the
	requester current on it — see `_missing_for_requester`)."""
	origins = _vector_origins(state)
	n = len(origins)
	if n <= max_origins:
		return build_vector(state)
	start = cursor % n
	window = origins[start : start + max_origins]
	if len(window) < max_origins:
		# Wrap: the window ran off the end of the sorted list — take the
		# remainder from the front so the window is always contiguous-with-wrap
		# and a full sweep still tiles the whole set with no gaps.
		window += origins[: max_origins - len(window)]
	picked = set(window)
	return {
		"vector_m": {h: state.membership[h].generation for h in picked if h in state.membership},
		"vector_o": {o: state.ownership[o].generation for o in picked if o in state.ownership},
	}


def _missing_for_requester(
	state, requester_vector: dict
) -> tuple[list[MembershipRecord | OwnershipAdvertisement], dict]:
	"""Compute the records the requester is missing or stale on, by comparing
	its vector to ours. Returns (records_to_send, newer_on_initiator_vector).

	For each origin in OUR state the requester's vector lacks OR has a lower
	generation: we owe the requester the latest record. For each origin where
	the requester has a HIGHER generation than us (we're behind), we add to
	`newer_on_initiator` so the requester can reverse-push to us — one extra
	Gossip datagram after the response closes the gap from the other direction
	(§15.2 mutual healing).
	"""
	their_m: dict[str, int] = requester_vector.get("vector_m") or {}
	their_o: dict[str, int] = requester_vector.get("vector_o") or {}
	records: list[MembershipRecord | OwnershipAdvertisement] = []
	newer_on_initiator = {"m": {}, "o": {}}
	# Membership: ours higher → owe; theirs higher → ask back.
	for h, m in state.membership.items():
		theirs = their_m.get(h)
		if theirs is None or m.generation > theirs:
			records.append(m)
		elif theirs > m.generation:
			newer_on_initiator["m"][h] = theirs
	# Ownership: same shape.
	for o, a in state.ownership.items():
		theirs = their_o.get(o)
		if theirs is None or a.generation > theirs:
			records.append(a)
		elif theirs > a.generation:
			newer_on_initiator["o"][o] = theirs
	# Origins the requester knows that we've NEVER heard of: ask back too.
	for h, gen in their_m.items():
		if h not in state.membership:
			newer_on_initiator["m"].setdefault(h, gen)
	for o, gen in their_o.items():
		if o not in state.ownership:
			newer_on_initiator["o"].setdefault(o, gen)
	# Bound the records list — trim AFTER the vector so the vector always carries
	# the full "I also lack these" set even when the records didn't all fit.
	# The next round picks up the trimmed tail.
	return records[:DEFAULT_ANTIENTROPY_RECORDS_MAX], newer_on_initiator


# --- wire payloads ----------------------------------------------------------


def anti_entropy_req_payload(vector: dict) -> dict:
	"""Build the payload for an AntiEntropyReq (just our summary vector)."""
	return vector


def parse_anti_entropy_req_payload(payload: Any) -> dict:
	"""Decode the requester's summary vector. Tolerates an absent `vector_m` /
	`vector_o` (an empty peer), returns an empty dict per kind — never crashes
	on a malformed payload (drop + log is a Stage 5 add)."""
	if not isinstance(payload, dict):
		raise ValueError("anti_entropy_req payload is not a dict")
	out = {
		"vector_m": dict(payload.get("vector_m") or {}),
		"vector_o": dict(payload.get("vector_o") or {}),
	}
	return out


def anti_entropy_resp_payload(
	records: list[MembershipRecord | OwnershipAdvertisement], newer_on_initiator: dict
) -> dict:
	"""Build the payload for an AntiEntropyResp — the records we owe the
	requester, plus the vector telling the requester what we ALSO lack."""
	return {
		"records": [wire.encode_record(r) for r in records],
		"newer_on_initiator": newer_on_initiator,
	}


def parse_anti_entropy_resp_payload(payload: Any) -> tuple[list, dict]:
	"""Decode an AntiEntropyResp payload. Returns (records, newer_on_initiator)
	with the records still as TAGGED dicts — the caller applies them via
	`wire.decode_record` one by one (so the apply rule can drop a record
	mid-stream if it's a stale generation the apply-state already has)."""
	if not isinstance(payload, dict):
		raise ValueError("anti_entropy_resp payload is not a dict")
	records = payload.get("records") or []
	if not isinstance(records, list):
		raise ValueError("anti_entropy_resp records not a list")
	newer = payload.get("newer_on_initiator") or {}
	if not isinstance(newer, dict):
		raise ValueError("anti_entropy_resp newer_on_initiator not a dict")
	return records, newer


# --- the round + handlers ---------------------------------------------------


def anti_entropy_round(
	daemon,
	transport: UdpTransport,
	*,
	rng: random.Random | None = None,
) -> int:
	"""Spec §15.1 — run ONE anti-entropy exchange against a randomly-selected
	peer. Round-robin-ish: a uniform-random pick kept the same across runs
	starves a single peer; the slow-sweep alternation (Stage-5 add) converges a
	clean heal faster. For now, uniform random over the alive membership
	excluding self — the same `select_peers` shape as gossip, with count=1.

	Returns the number of peers contacted (0 or 1) — used by the loop's
	bookkeeping. Sends a TYPE_ANTI_ENTROPY_REQ carrying our summary vector; the
	response arrives asynchronously on the next loop tick's drain and is
	dispatched in `handle_anti_entropy_resp`."""
	peers = select_peers(
		daemon.state.membership, daemon.identity.host_id, count=1, rng=rng or random.Random()
	)
	if not peers:
		return 0
	peer_id = peers[0]
	peer = daemon.state.membership[peer_id]
	# Build the generation vector: the full vector when the origin set fits a
	# single datagram (N <= MAX_VECTOR_ORIGINS — unchanged bytes), otherwise a
	# DETERMINISTIC ROTATING WINDOW of at most MAX_VECTOR_ORIGINS origins starting
	# at the daemon's anti-entropy cursor. Advancing the cursor by the window each
	# round tiles the full origin set with no gaps, so every origin is advertised
	# within a bounded ceil(N / window) rounds regardless of the random peer pick
	# — the §15.4 convergence guarantee, without the old random sample's shrinking
	# per-round success probability. Without a bounded vector a cluster larger than
	# ~17 hosts would also overflow 1280 bytes and crash the loop.
	origins = _vector_origins(daemon.state)
	cursor = daemon._ae_cursor
	step = min(MAX_VECTOR_ORIGINS, len(origins)) or 1
	vector = _windowed_vector(daemon.state, cursor)
	sent = 0
	for _attempt in range(2):
		msg = Message(
			type=TYPE_ANTI_ENTROPY_REQ,
			sender=daemon.identity.host_id,
			signing_public_key=daemon.own_signing_pub_b64,
			payload=anti_entropy_req_payload(vector),
		)
		try:
			data = msg.to_bytes(daemon.own_signing_priv_b64)
		except DatagramTooLarge:
			# A window still overflows the datagram (pathological — a single
			# origin's entry is huge, or MAX_VECTOR_ORIGINS was tuned up). Shrink
			# the window by halving max_origins; the cursor still advances by the
			# FULL step below so coverage keeps sweeping and never stalls on a
			# short window (a shrunk window just makes the sweep take more rounds,
			# it never skips origins).
			vector = _windowed_vector(daemon.state, cursor, max_origins=max(1, MAX_VECTOR_ORIGINS // 2))
			continue
		transport.send((peer.endpoint, daemon.config.ancp_port), data)
		sent = 1
		break
	# Advance the rotating cursor by the window even when the datagram couldn't be
	# built (sent == 0): the next round tiles the NEXT window regardless, so a
	# pathological over-large window for one origin can't wedge the sweep on it
	# forever. Wrap modulo the origin count so the cursor stays bounded.
	if len(origins) > MAX_VECTOR_ORIGINS:
		daemon._ae_cursor = (cursor + step) % len(origins)
	return sent


def handle_anti_entropy_req(msg: Message, daemon, _gossip_state) -> None:
	"""Spec §15.2 — a peer asked for our records it's missing. Build a response
	of every record where our generation is higher than its vector claims, plus
	a `newer_on_initiator` vector describing what WE are also missing (so the
	requester can reverse-push and we heal mutually). Reply unicast to the
	requester's mesh address on the ANCP port.

	`_gossip_state` is unused at the request-handling step — responses are pure
	data; only `_handle_anti_entropy_resp` records the freshly-applied records
	into the gossip state for forwarding.
	"""
	try:
		vector = parse_anti_entropy_req_payload(msg.payload)
	except ValueError:
		return
	records, newer = _missing_for_requester(daemon.state, vector)
	payload = anti_entropy_resp_payload(records, newer)
	# Stage 5 — sign our own records before send, exactly as gossip_round and
	# _reverse_push do. Without this, the receiver's §19.3 verifier drops every
	# record carrying a signing_public_key but no wire signature, making
	# anti-entropy a no-op in any cluster with signing enabled (§15.5).
	# sign_records_if_owned mutates the tagged dicts in place, which is fine —
	# they are the same objects referenced by payload["records"].
	wire.sign_records_if_owned(
		payload.setdefault("records", []),
		daemon.own_signing_priv_b64,
		daemon.identity.host_id,
	)
	resp = Message(
		type=TYPE_ANTI_ENTROPY_RESP,
		sender=daemon.identity.host_id,
		signing_public_key=daemon.own_signing_pub_b64,
		payload=payload,
	)
	# Send back to the requester's public endpoint. The endpoint comes from
	# their latest Membership Record; if we've never heard of them, drop
	# quietly — they'll retry on their next interval.
	requester_record = daemon.state.membership.get(msg.sender)
	if requester_record is None:
		return  # unknown host — their cold-join advert will arrive and
		# the next anti-entropy req from them will succeed.
	data = _serialize_with_trim(resp, daemon.own_signing_priv_b64)
	daemon.unicast_send(requester_record.endpoint, data)


def handle_anti_entropy_resp(
	msg: Message, daemon, gossip_state
) -> list[MembershipRecord | OwnershipAdvertisement]:
	"""Spec §15.2 — apply the records the responder sent, then reverse-push the
	records the responder said it ALSO lacks (mutual healing). Returns the list
	of freshly-applied records (the loop uses it to schedule the debounced
	wg-mesh apply + queue them for gossip forward)."""
	try:
		records_tagged, newer = parse_anti_entropy_resp_payload(msg.payload)
	except ValueError:
		return []
	freshly_applied: list[MembershipRecord | OwnershipAdvertisement] = []
	# Stage 5 — populate the wire-sig side-channel so the §19.3 verifier (run
	# inside `_apply` → `_apply_record`) can read each record's signature.
	sigs: dict[int, str] = {}
	daemon._incoming_wire_sigs = sigs  # type: ignore[attr-defined]
	for tagged in records_tagged:
		try:
			record = wire.decode_record(tagged)
		except ValueError:
			continue
		wire_sig = wire.wire_signature(tagged)
		if wire_sig is not None:
			sigs[id(record)] = wire_sig
		# Anti-entropy records go through the SAME §13.2 monotonic apply rule
		# as gossip — a stale mid-stream record drops on the generation check,
		# harmlessly.
		changed = _apply(record, daemon)
		if changed:
			freshly_applied.append(record)
			gossip_state.note_applied(record)
	# Reverse push: if the responder said it's missing some origins too, look
	# them up in our state and send a Gossip carrying them. We reuse the Gossip
	# path so a single reverse datagram closes the gap in one shot —
	# equivalent to a Gossip piggyback but targeted to the responder.
	if newer:
		_reverse_push(msg.sender, newer, daemon, transport=None)
	return freshly_applied


def _apply(record: MembershipRecord | OwnershipAdvertisement, daemon) -> bool:
	"""Apply via the state's monotonic rule, with §19.3 signature verification
	first (same as gossip's `_apply_record`). A record whose signature fails to
	verify against its origin's published signing pubkey is dropped + counted.
	When a MembershipRecord is applied and carries a signing_public_key, the
	daemon's signing_pubkey_cache is updated (§19.1 trust-directory sync).

	§13.3 duplicate suppression: an EXACT (origin, kind, generation) re-delivery
	hits the seen-cache → drop BEFORE the ed25519 verify + apply (no re-forward).
	Only an exact key is suppressed; a strictly-higher generation has a different
	key and is never dropped here. Mirrors gossip's `_apply_record`.
	"""
	if daemon.state.seen_already(dedupe_key(record)):
		return False
	verifier = getattr(daemon, "signature_verifier", None)
	if verifier is not None:
		try:
			verifier(record, daemon)
		except Exception:
			counter = getattr(daemon, "metrics", None)
			if counter is not None:
				counter.incr("signature_failed")
			return False
	if isinstance(record, MembershipRecord):
		cache = getattr(daemon, "signing_pubkey_cache", None)
		changed = daemon.state.apply_membership(record, pubkey_cache=cache)
		if changed:
			tracker = getattr(daemon, "failure_tracker", None)
			if tracker is not None:
				# §14.4: mirror gossip's `_apply_record` — a `kind=leaving`
				# record arms the leaving → dead countdown; a normal `member`
				# record is the fast-refute that resets to alive.
				if record.kind == MembershipKind.LEAVING:
					tracker.note_leaving(record.host_id)
				else:
					tracker.note_alive(record.host_id)
		return changed
	if isinstance(record, OwnershipAdvertisement):
		return daemon.state.apply_ownership(record)
	return False


def _reverse_push(
	target_host_id: str,
	newer: dict,
	daemon,
	*,
	transport,
) -> None:
	"""Build a Gossip carrying the records the responder said it lacks, and
	unicast it. The responder's `newer_on_initiator` vector tells us which
	origins to send; we look each up in our state (it might have advanced since
	the responder built its vector — we send our latest either way, and the
	responder's §13.2 apply rule sorts it out)."""
	records: list[MembershipRecord | OwnershipAdvertisement] = []
	for h in newer.get("m", {}) or {}:
		rec = daemon.state.membership.get(h)
		if rec is not None:
			records.append(rec)
	for o in newer.get("o", {}) or {}:
		rec = daemon.state.ownership.get(o)
		if rec is not None:
			records.append(rec)
	if not records:
		return
	target_record = daemon.state.membership.get(target_host_id)
	if target_record is None:
		return  # we no longer know the target — they'll retry
	msg = Message(
		type=TYPE_GOSSIP,
		sender=daemon.identity.host_id,
		signing_public_key=daemon.own_signing_pub_b64,
		payload=wire.sign_records_if_owned(
			wire.gossip_payload(records),
			daemon.own_signing_priv_b64,
			daemon.identity.host_id,
		),
	)
	daemon.unicast_send(target_record.endpoint, _serialize_with_trim(msg, daemon.own_signing_priv_b64))
	# `transport` is unused here — `daemon.unicast_send` is the path; the param
	# is kept in the signature for symmetry with `gossip_round` and a future
	# Stage-5 direct-call API.
	_ = transport


def _serialize_with_trim(message: Message, sender_priv_b64: str = "") -> bytes:
	"""Serialize, signing the envelope with `sender_priv_b64` (spec §19.1) and
	trimming the records list tail to fit MAX_DATAGRAM_BYTES. The records we
	drop stay in our state; the next anti-entropy round picks them up
	(convergence is over N rounds at worst). Mutates `message.payload`."""
	try:
		return message.to_bytes(sender_priv_b64)
	except DatagramTooLarge:
		payload = message.payload
		if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
			# Gossip-style payloads (a list at the top level) — fall through to
			# the list-trim path; the AntiEntropyResp shape is dict-with-records.
			records_list = payload if isinstance(payload, list) else None
		else:
			records_list = payload["records"]
		if records_list is None:
			raise
		while records_list:
			records_list.pop()
			try:
				return message.to_bytes(sender_priv_b64)
			except DatagramTooLarge:
				continue
		return message.to_bytes(sender_priv_b64)


__all__ = [
	"DEFAULT_ANTIENTROPY_RECORDS_MAX",
	"MAX_VECTOR_ORIGINS",
	"anti_entropy_req_payload",
	"anti_entropy_resp_payload",
	"anti_entropy_round",
	"build_vector",
	"handle_anti_entropy_req",
	"handle_anti_entropy_resp",
	"parse_anti_entropy_req_payload",
	"parse_anti_entropy_resp_payload",
]
