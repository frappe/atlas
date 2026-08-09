"""Gossip round + incoming-message handler (spec §13).

Two responsibilities:

- `gossip_round(daemon, transport)` — runs every `gossip_interval` (the same
  cadence as the main loop tick): select `gossip_fanout` peers, build a
  `TYPE_GOSSIP` message carrying the most-recently-applied records (piggyback
  -- capped to fit `MAX_DATAGRAM_BYTES`), and send to each selected peer.

- `handle_message(msg, sender_addr, daemon)` — dispatch an incoming message:
  apply each piggybacked record via `Daemon.state.apply_*` (which use the
  §13.3 dedupe cache + the §13.2 monotonic Generation rule), and queue the
  freshly-applied ones for forwarding in the next gossip round. Rejected
  duplicates are dropped silently (no re-forward); rejected lower-generations
  are NOT re-added (Issue C close-out — they can never compete cross-origin).

Stage 2 handles `TYPE_GOSSIP` + `TYPE_MEMBERSHIP_ADVERT` (the cold-join unicast
of §9.1). SWIM probes (`ping`/`ack`/`indirect_ping`) and anti-entropy requests
land in stages 3/4 — their wire types are declared in `wire.py` so a datagram
of an unimplemented type is dropped with a debug log (a peer running a newer
build shouldn't crash this loop, and an old peer running an obsolete build
shouldn't deadlock the loop).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from . import wire
from .antientropy import handle_anti_entropy_req, handle_anti_entropy_resp
from .peers import HostID, select_peers
from .records import (
	MembershipKind,
	MembershipRecord,
	OwnershipAdvertisement,
	dedupe_key,
)
from .transport import UdpTransport
from .wire import (
	TYPE_ACK,
	TYPE_ANTI_ENTROPY_REQ,
	TYPE_ANTI_ENTROPY_RESP,
	TYPE_GOSSIP,
	TYPE_INDIRECT_PING,
	TYPE_MEMBERSHIP_ADVERT,
	TYPE_PING,
	DatagramTooLarge,
	Message,
)

# The default max piggyback records per gossip message before we start trimming
# for datagram size. A small number is right: gossip is the low-latency hot
# path; large spreads ride anti-entropy. We sort the queue by recency so the
# freshest records survive the trim (the ones a peer most likely lacks).
DEFAULT_PIGGYBACK_MAX = 16


@dataclass(slots=True)
class GossipState:
	"""The per-host gossip bookkeeping the daemon owns. Holds:

	- `forward_queue` — records we OWE our peers (just applied locally; we
	  forward each at the next gossip round to `gossip_fanout` peers). Bounded
	  by `gossip_forward_budget` per the §13.2 last paragraph.
	- `recently_applied` — an MRU list so the gossip round's piggyback prefers
	  the freshest records (the ones a peer is most likely missing). Bounded by
	  `DEFAULT_PIGGYBACK_MAX`.
	"""

	forward_queue: list[MembershipRecord | OwnershipAdvertisement] = field(default_factory=list)
	recently_applied: list[MembershipRecord | OwnershipAdvertisement] = field(default_factory=list)

	def note_applied(self, record: MembershipRecord | OwnershipAdvertisement) -> None:
		"""A record was applied locally (either originated here or relayed by a
		peer and not seen before). We owe it to our peers (forward queue) and
		it's the freshest thing we have for the next piggyback."""
		# Forward queue is a list, not a set — ordering matters (FIFO by recency
		# so the oldest "owed" record goes out first; a peer starved of updates
		# gets the oldest pending one). Cap at the forward budget.
		self.forward_queue.append(record)
		# Recently-applied is a simple LRU; a re-arrival of the same record just
		# reasserts its position at the head (a no-op on the union-of-latest
		# table, but the piggyback prefers it again so stragglers catch up).
		self.recently_applied = [record, *[r for r in self.recently_applied if r != record]][
			:DEFAULT_PIGGYBACK_MAX
		]

	def drain_forward(self, budget: int) -> list[MembershipRecord | OwnershipAdvertisement]:
		"""Pop the next up to `budget` records we owe. Called by the gossip
		round; the records leave the forward queue (we'll re-add if a later
		missing-ack path learns a peer didn't get them — Stage 3 anti-entropy is
		the reliable backstop for now)."""
		taken = self.forward_queue[:budget]
		self.forward_queue = self.forward_queue[budget:]
		return taken


def gossip_round(
	daemon,
	transport: UdpTransport,
	gossip_state: GossipState,
	*,
	select_fn: Callable[..., list[HostID]] = select_peers,
) -> int:
	"""Run one gossip fan-out (spec §13.1): pick `gossip_fanout` peers, build a
	piggyback from `recently_applied` (preferring freshest) fused with
	`forward_queue` (oldest owed), trim to fit `MAX_DATAGRAM_BYTES`, send to each
	peer. Returns the number of peers contacted. A lone host (no peers) returns
	0 — no traffic, no log spam every tick."""
	peers = select_fn(daemon.state.membership, daemon.identity.host_id, daemon.config.gossip_fanout)
	if not peers:
		return 0
	# The piggyback is the union of "freshest applied" and "owed to peers"; we
	# cap by DEFAULT_PIGGYBACK_MAX + budget and let `build_message` trim further
	# for the datagram budget.
	piggyback = _pick_piggyback(gossip_state, daemon.config.gossip_forward_budget)
	if not piggyback:
		# We have nothing new. Still send a Gossip with an empty piggyback — it
		# serves as our heartbeat presence (§14 piggybacks on gossip) and lets a
		# peer's recv path confirm we're alive. SWIM Stage 4 will formalize the
		# ping-but-here we still produce an envelope.
		pass  # build_message will emit an empty payload
	message = Message(
		type=TYPE_GOSSIP,
		sender=daemon.identity.host_id,
		signing_public_key=daemon.own_signing_pub_b64,
		payload=wire.sign_records_if_owned(
			wire.gossip_payload(piggyback),
			daemon.own_signing_priv_b64,
			daemon.identity.host_id,
		),
	)
	data = _serialize_with_trim(message, daemon.own_signing_priv_b64)
	for peer_id in peers:
		peer = daemon.state.membership[peer_id]
		transport.send((peer.endpoint, daemon.config.ancp_port), data)
	return len(peers)


def _pick_piggyback(
	gossip_state: GossipState, forward_budget: int
) -> list[MembershipRecord | OwnershipAdvertisement]:
	"""Union the freshly-applied (freshest-first) with the forwarded-owed
	(oldest-first), deduping; cap at a sensible round size (`DEFAULT_PIGGYBACK_MAX`
	* 2 so we don't truncate the freshest before sending them)."""
	owed = gossip_state.drain_forward(forward_budget)
	recent = gossip_state.recently_applied
	seen: set[tuple] = set()
	out: list[MembershipRecord | OwnershipAdvertisement] = []
	for r in [*owed, *recent]:
		key = _record_key(r)
		if key in seen:
			continue
		seen.add(key)
		out.append(r)
	return out[: DEFAULT_PIGGYBACK_MAX * 2]


def _record_key(r: MembershipRecord | OwnershipAdvertisement) -> tuple:
	"""A stable equality key for a record, used to dedupe the piggyback union.
	Matches the §13.3 dedupe key shape (origin/kind/generation) but lives here
	because this is about the *piggyback pick*, not the apply-time dedupe."""
	if isinstance(r, MembershipRecord):
		return ("m", r.host_id, r.generation)
	return ("o", r.origin, r.generation)


def _serialize_with_trim(message: Message, sender_priv_b64: str = "") -> bytes:
	"""Serialize `message`, signing the envelope with `sender_priv_b64` (spec
	§19.1) and trimming the piggyback tail if the datagram would overflow
	`MAX_DATAGRAM_BYTES`. We mutate the payload (a list of tagged dicts) in
	place by popping the end until `to_bytes` succeeds; the dropped records
	stay in the apply state (we don't lose them) and they re-enter the
	piggyback next round via `recently_applied` (MRU preservation).

	`sender_priv_b64 == ""` skips signing (the in-test path; production always
	signs) — the envelope goes out unsigned and the receiver's
	`envelope_verifier` would drop it, but tests that install no verifier
	still pass."""
	try:
		return message.to_bytes(sender_priv_b64)
	except DatagramTooLarge:
		payload = message.payload
		if not isinstance(payload, list):
			raise
		while payload:
			payload.pop()  # drop the tail (least-recent record)
			try:
				return message.to_bytes(sender_priv_b64)
			except DatagramTooLarge:
				continue
		# Even an empty piggyback overflowed — would mean the envelope itself
		# is over 1280 bytes, which is impossible at our record sizes. Let it
		# fail loud so we surface it.
		return message.to_bytes(sender_priv_b64)


def handle_message(msg: Message, sender_addr: tuple[str, int], daemon, gossip_state: GossipState) -> None:
	"""Dispatch one incoming ANCP message (spec §13.2 / §9.1). The §19.1
	transport binding check verifies the UDP datagram's source address matches
	the claimed sender's public endpoint from our membership table. Unknown
	senders are only allowed for TYPE_MEMBERSHIP_ADVERT (cold-join from a
	newcomer). After transport binding, the record's ed25519 signature is
	verified per-apply (§19.3).
	"""
	# §19.1 transport binding: for a KNOWN sender, the UDP source IP must
	# match the sender's public endpoint from our membership table (defense
	# against trivial IP-spoofing). Unknown senders are accepted regardless
	# — the §19.3 ed25519 record signatures are the real defense against
	# forgery. For MEMBERSHIP_ADVERT (cold-join) we additionally verify that
	# the newcomer's claimed endpoint matches the UDP source, a basic spoof
	# check before the record is applied and signatures become mandatory.
	sender_record = daemon.state.membership.get(msg.sender)
	if sender_record is not None:
		if sender_addr[0] != sender_record.endpoint:
			return  # spoofed source address — drop
	elif msg.type == TYPE_MEMBERSHIP_ADVERT:
		try:
			newcomer_endpoint = wire.parse_membership_advert_payload(msg.payload).endpoint
		except ValueError:
			return
		if sender_addr[0] != newcomer_endpoint:
			return
	if msg.type == TYPE_GOSSIP:
		_handle_gossip(msg, daemon, gossip_state)
	elif msg.type == TYPE_MEMBERSHIP_ADVERT:
		_handle_membership_advert(msg, daemon, gossip_state)
	elif msg.type == TYPE_ANTI_ENTROPY_REQ:
		# Spec §15.2 — a peer asks us for records it's missing. Build the
		# response from our state vs the peer's vector + unicast-reply.
		handle_anti_entropy_req(msg, daemon, gossip_state)
	elif msg.type == TYPE_ANTI_ENTROPY_RESP:
		# Spec §15.2 — a peer answered our pull request. Apply the records +
		# reverse-push what the responder said it also lacks (mutual healing).
		handle_anti_entropy_resp(msg, daemon, gossip_state)
	elif msg.type in (TYPE_PING, TYPE_ACK, TYPE_INDIRECT_PING):
		# Spec §14.2 — SWIM probes. The daemon has a `probe_protocol` wired by
		# `main.py` (Stage 4); route to its handlers. If `probe_protocol` is
		# None (a test that didn't wire it), drop quietly — same posture as
		# the Stage-2 placeholder.
		probe_protocol = getattr(daemon, "probe_protocol", None)
		if probe_protocol is None:
			_ = msg
			return
		if msg.type == TYPE_PING:
			probe_protocol.handle_ping(msg, daemon, daemon.transport)
		elif msg.type == TYPE_ACK:
			probe_protocol.handle_ack(msg, daemon, daemon.transport)
		elif msg.type == TYPE_INDIRECT_PING:
			probe_protocol.handle_indirect_ping(msg, daemon, daemon.transport)
	else:
		# Unknown type — could be a future ANCP version we don't speak. Drop,
		# don't crash. Stage 5 surfaces a counter.
		_ = msg
	_ = sender_addr  # reserved for the Stage 5 wg-identity match check


def _handle_gossip(msg: Message, daemon, gossip_state: GossipState) -> None:
	"""Apply each piggybacked record (§13.2); note freshly-applied ones in the
	forward queue + the MRU."""
	# Stage 5 — populate the wire-sig side-channel so `_apply_record`'s
	# verifier can read each record's signature without modifying the frozen
	# slots dataclass.
	sigs: dict[int, str] = {}
	daemon._incoming_wire_sigs = sigs
	try:
		records = wire.parse_gossip_payload(msg.payload, sigs_target=sigs)
	except ValueError:
		return  # malformed datagram — drop + retrieve
	# §19.3 — apply MembershipRecords before OwnershipAdvertisements within a
	# batch. An OwnershipAdvertisement can only be verified once its origin's
	# MembershipRecord (carrying the signing key) is applied; ordering the pair
	# so membership lands first lets a co-delivered pair verify on the first
	# delivery instead of dropping the ownership and waiting for anti-entropy to
	# re-pull it. A stable sort preserves the per-kind wire order. Correctness
	# does not depend on this ordering — an out-of-order or split delivery is
	# still healed by the drop + re-pull path (the ownership isn't applied, so
	# its gen-vector never advances and the next round re-delivers it).
	for record in sorted(records, key=lambda r: 0 if isinstance(r, MembershipRecord) else 1):
		changed = _apply_record(record, daemon)
		if changed:
			gossip_state.note_applied(record)


def _apply_record(record: MembershipRecord | OwnershipAdvertisement, daemon) -> bool:
	"""Apply via the state's monotonic rule (§13.2); the apply also marks the
	dedupe cache (§13.3). For a Membership Record that wins (higher gen from
	an origin), the §14 fast-refute trigger fires (Stage 4). For ANY record,
	Stage 5's ed25519 signature verify (§19.3) runs first via the daemon's
	`signature_verifier` injection — a record whose signature fails to verify
	against its origin's published signing pubkey is dropped + counted, never
	applied. A record with NO signature field is accepted iff the daemon's
	verifier is None (the in-test / pre-Stage-5 path); in production every
	genuine record is signed and the verifier rejects unsigned ones.

	When a MembershipRecord is applied and carries a signing_public_key, the
	daemon's signing_pubkey_cache is updated (§19.1 trust-directory sync).

	§13.3 duplicate suppression: BEFORE the expensive per-record ed25519 verify
	and the apply, check the seen-cache on the record's (origin, kind, generation)
	key. An EXACT re-delivery hits → drop silently (no verify, no apply, no
	re-forward) and return "unchanged". This gives a cheap pre-verify drop for
	replayed/re-delivered records (a DoS-posture win). Only an exact key matches:
	a strictly-higher generation from the same origin has a DIFFERENT key, so it
	is never suppressed — the generation check at apply still gates it. `_mark_seen`
	runs inside `apply_*` on the miss path, so a record that verify defers (e.g.
	an OwnershipAdvertisement whose origin's signing key isn't known yet) is NOT
	cached and stays re-deliverable."""
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
				# §14.4: a `kind=leaving` record is a graceful-shutdown notice —
				# arm the leaving → dead countdown, do NOT reset the origin to
				# alive (that would resurrect the departing host). A normal
				# `member` record is the §14.2/§14.5 fast-refute: reset to alive.
				if record.kind == MembershipKind.LEAVING:
					tracker.note_leaving(record.host_id)
				else:
					tracker.note_alive(record.host_id)
		return changed
	if isinstance(record, OwnershipAdvertisement):
		return daemon.state.apply_ownership(record)
	return False


def _handle_membership_advert(msg: Message, daemon, gossip_state: GossipState) -> None:
	"""The cold-join unicast (§9.1 step 4): the newcomer sent its own Membership
	Record to us (a seed). We apply it locally (adds newcomer as a wg-mesh
	peer via the next atomic apply), and reply with a Gossip carrying our own
	latest Membership Record + a bundle of EVERY OTHER member's latest record
	(state transfer — fast-paths the antientropy fill the newcomer would
	otherwise wait one round for).

	Stage 2 returns the bundle as a unicast reply rather than a regular gossip
	round (we want the newcomer to get the full table on its first contact, not
	whenever our random peer selection happens to fan out to it)."""
	try:
		newcomer_record = wire.parse_membership_advert_payload(msg.payload)
	except ValueError:
		return
	# §19.3: thread the wire signature from the payload dict into the
	# side-channel so `_apply_record`'s verifier can find it.
	sigs: dict[int, str] = {}
	wire_sig = msg.payload.get("signature") if isinstance(msg.payload, dict) else None
	if isinstance(wire_sig, str):
		sigs[id(newcomer_record)] = wire_sig
	daemon._incoming_wire_sigs = sigs  # type: ignore[attr-defined]
	changed = _apply_record(newcomer_record, daemon)
	if changed:
		gossip_state.note_applied(newcomer_record)
		# Bundle = own Membership Record + every OTHER member's latest. The
		# newcomer applies all of them via the same monotonic rule.
		own = daemon.state.membership.get(daemon.identity.host_id)
		others = [
			daemon.state.membership[h]
			for h in daemon.state.membership
			if h != daemon.identity.host_id and h != newcomer_record.host_id
		]
		bundle = [own, *others] if own is not None else others
		advert_reply = Message(
			type=TYPE_GOSSIP,  # reuse Gossip — the newcomer's recv path is identical
			sender=daemon.identity.host_id,
			signing_public_key=daemon.own_signing_pub_b64,
			payload=wire.sign_records_if_owned(
				wire.gossip_payload(bundle),
				daemon.own_signing_priv_b64,
				daemon.identity.host_id,
			),
		)
		# Send back to the newcomer's public endpoint on the same ANCP port.
		# ANCP rides plain UDP (not over wg-mesh), so the newcomer receives
		# this on its own ANCP socket bound to its public endpoint.
		daemon.unicast_send(
			newcomer_record.endpoint,
			_serialize_with_trim(advert_reply, daemon.own_signing_priv_b64),
		)


__all__ = [
	"DEFAULT_PIGGYBACK_MAX",
	"GossipState",
	"gossip_round",
	"handle_message",
]
