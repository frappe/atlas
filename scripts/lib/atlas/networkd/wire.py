"""Wire format for ANCP messages (spec §13 / §9 / §19).

ANCP rides plain UDP on each host's public IPv6 endpoint, port 7946 (spec §5).
The implementation deliberately diverges from spec drafts that asserted
transport over wg-mesh — wg-mesh is the *output* of the control plane (the
data-plane device the daemon programs in §16), not its transport; ANCP must
bootstrap before the data plane it produces.

Stage 2 uses a JSON envelope: debuggable, easy to evolve, and small enough at
our record sizes that the encode/decode cost is irrelevant over a 200 ms gossip
tick. Binary is a §20 optimization held off until profiling proves it needs to
be — Taste.md's "don't import, copy" rule applies (no protobuf/cbor dep).

Envelope (spec §19.1):

    Message = {
      "type"               : "gossip" | "membership_advert" | "anti_entropy_req" | ...,
      "sender"             : HostID,                # the origin of this datagram
      "signing_public_key" : base64,                # the sender's ed25519 pubkey
      "payload"            : <type-specific JSON>,  # records, summaries, etc.
      "signature"          : base64,                # ed25519 over {type, sender,
                                                    #          signing_public_key, payload}
      "introduction_signature": base64 | omitted    # §19.5 newcomer cert (only the
                                                    # first MembershipAdvertisement
                                                    # from an unknown host_id)
    }

Datagram sizes are bounded by the IPv6 minimum MTU payload floor (1280 B); we
cap a serialized Gossip piggyback to fit that, dropping the tail when it would
overflow. Larger state transfers ride anti-entropy (§15, Stage 3) which can use
a multi-datagram or TCP exchange — gossip is the hot-path small-burst channel,
not the bulk channel.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .records import (
	MembershipRecord,
	OwnershipAdvertisement,
)
from .signing import SignatureError

# The maximum serialized Message size we'll send or accept over a single ANCP
# UDP datagram. ANCP rides plain UDP on the public IPv6 endpoint (§5); the IPv6
# minimum MTU payload floor is 1280 B, so a datagram never fragments even if a
# routed path shrinks the effective MTU further. The apply-path recv loop
# rejects oversized datagrams at the kernel boundary (no parse work — drops +
# counts `oversized_drops`). Larger state rides anti-entropy (§15).
MAX_DATAGRAM_BYTES = 1280

# Message types. SWIM probe types (§14) and anti-entropy (§15) are declared here
# so the dispatch table is complete, but their handlers land in stages 3/4 —
# Stage 2 wires gossip + the cold-join `membership_advert`. A handler that
# receives an unimplemented type drops the message and logs (fail-soft for an
# unknown type from a newer peer, never crash the loop on a malformed byte).
TYPE_GOSSIP = "gossip"
TYPE_MEMBERSHIP_ADVERT = "membership_advert"  # unicast cold-join (§9.1 step 4)
TYPE_PING = "ping"  # §14.2 — Stage 4
TYPE_ACK = "ack"  # §14.2 — Stage 4
TYPE_INDIRECT_PING = "indirect_ping"  # §14.2 — Stage 4
TYPE_ANTI_ENTROPY_REQ = "anti_entropy_req"  # §15 — Stage 3
TYPE_ANTI_ENTROPY_RESP = "anti_entropy_resp"  # §15 — Stage 3


@dataclass(frozen=True, slots=True)
class Message:
	"""One ANCP message envelope (spec §19.1 — the signed-envelope shape). The
	`sender` field is the HostID of the host that emitted this datagram (the
	*relay*, for a gossip message forwarding another origin's records); the
	receiver verifies the envelope signature against its cached signing pubkey
	for `sender` before any payload work. `signing_public_key` rides the wire so
	a first-contact receiver (one with no cached pubkey for `sender` yet) can
	verify the envelope against the self-asserted pubkey — but only when paired
	with an `introduction_signature` (spec §19.5) that proves the binding to the
	operator's provision key. In every other case the receiver drops a datagram
	whose `sender` is not in its trust directory.

	`signature` is the whole-envelope ed25519 signature over the canonical body
	`{type, sender, signing_public_key, payload}` (see `signing.sign_envelope`).
	`introduction_signature` is present ONLY on the first MembershipAdvertisement
	from a previously-unknown `host_id`; it is the operator's signature over
	`{host_id, signing_public_key, generation}` (spec §19.5)."""

	type: str
	sender: str
	payload: Any
	signing_public_key: str = ""
	signature: str = ""
	introduction_signature: str = ""

	def envelope_signed_body(self) -> dict:
		"""The canonical dict that the envelope signature covers — the four
		envelope fields, NO `signature`/`introduction_signature`. Used by both
		`sign_envelope` (sign) and `verify_envelope` (verify) so the bytes a
		 signer signs are byte-identical to the bytes the verifier reconstructs.
		"""
		return {
			"type": self.type,
			"sender": self.sender,
			"signing_public_key": self.signing_public_key,
			"payload": self.payload,
		}

	def introduction_signed_body(self) -> dict:
		"""The canonical dict that the `introduction_signature` covers —
		`{host_id, signing_public_key, generation}` from the MembershipRecord
		inside the payload. Returns `{}` if the payload is not a single
		MembershipRecord dict (in which case `introduction_signature` must be
		empty and is ignored)."""
		if not self.introduction_signature:
			return {}
		# the introduction binds the MembershipRecord's (host_id,
		# signing_public_key, generation) — those are looked up from the
		# payload's wire dict, not the envelope fields.
		if not isinstance(self.payload, dict):
			return {}
		host_id = self.payload.get("host_id")
		signing_public_key = self.payload.get("signing_public_key")
		generation = self.payload.get("generation")
		if host_id is None or signing_public_key is None or generation is None:
			return {}
		return {
			"host_id": host_id,
			"signing_public_key": signing_public_key,
			"generation": int(generation),
		}

	def to_bytes(self, sender_priv_b64: str = "") -> bytes:
		"""Serialize to UTF-8 JSON bytes (compact, sort_keys). Signs the
		envelope with `sender_priv_b64` (spec §19.1) before serializing; if
		`self.signature` is already set (a pre-signed message passed through),
		uses that. Raises `DatagramTooLarge` if the serialized form exceeds
		`MAX_DATAGRAM_BYTES` — the gossip handler catches and trims the
		piggyback tail.

		If `sender_priv_b64` is empty AND `self.signature` is empty, emits an
		UNSIGNED envelope (the test path — production always passes a priv
		key). The receiver's `envelope_verifier` (`daemon.py`) rejects any
		unsigned datagram in production."""
		from . import signing

		sig = self.signature
		if sender_priv_b64:
			sig = signing.sign_envelope(self.envelope_signed_body(), sender_priv_b64)
		d: dict[str, Any] = {
			"type": self.type,
			"sender": self.sender,
			"signing_public_key": self.signing_public_key,
			"payload": self.payload,
			"signature": sig,
		}
		if self.introduction_signature:
			d["introduction_signature"] = self.introduction_signature
		body = json.dumps(d, separators=(",", ":"), sort_keys=True).encode("utf-8")
		if len(body) > MAX_DATAGRAM_BYTES:
			raise DatagramTooLarge(len(body))
		return body

	def verify_envelope(self, signing_pub_b64: str) -> None:
		"""Verify the envelope signature against the supplied pubkey (the
		cached signing pubkey for `sender`, OR the self-asserted
		`signing_public_key` if the caller has none cached). Raises
		`SignatureError` on any failure. The caller selects which pubkey to
		pass — the wire layer does not consult the daemon's trust directory."""
		from . import signing

		signing.verify_envelope(self.envelope_signed_body(), self.signature, signing_pub_b64)

	def verify_introduction(self, operator_pub_b64: str) -> None:
		"""Verify the `introduction_signature` against the operator's provision
		pubkey (spec §19.5). Raises `SignatureError` if the cert is absent or
		invalid. The caller passes the operator pubkey (seeded to every host,
		spec §19.4)."""
		from . import signing

		if not self.introduction_signature:
			raise SignatureError("message carries no introduction_signature")
		signing.verify_introduction(
			self.introduction_signed_body(), self.introduction_signature, operator_pub_b64
		)


class DatagramTooLarge(RuntimeError):
	"""Raised by `Message.to_bytes` when the serialized form would overflow the
	IPv6-minimum-MTU payload budget (§5). The gossip handler catches it and
	trims the piggyback (by dropping tail records) until it fits — see
	`gossip.trim_to_fit`. The recv path (`transport.drain`) mirrors the cap by
	dropping oversized inbound datagrams at the kernel boundary."""


def from_bytes(data: bytes) -> Message:
	"""Deserialize a UDP datagram body. Raises `ValueError` on malformed JSON
	or a missing `type`/`sender` (fail loud at the boundary — a malformed
	datagram from a peer should not be silently dropped, since it usually means
	a version skew the operator needs to know about).

	NOTE: this parses the envelope but does NOT verify the signature. The
	receiver must call `Message.verify_envelope(cached_pub)` before any payload
	work — typically in the loop's recv path via the daemon's
	`envelope_verifier` (`daemon.py:default_envelope_verifier`)."""
	obj = json.loads(data.decode("utf-8"))
	if not isinstance(obj, dict):
		raise ValueError("ANCP message is not a JSON object")
	if "type" not in obj or "sender" not in obj:
		raise ValueError("ANCP message missing type/sender")
	return Message(
		type=obj["type"],
		sender=obj["sender"],
		payload=obj.get("payload"),
		signing_public_key=obj.get("signing_public_key", ""),
		signature=obj.get("signature", ""),
		introduction_signature=obj.get("introduction_signature", ""),
	)


# --- record (de)serializers (shared with state.py — kept pure & I/O-free) -----
# The on-wire shape for a MembershipRecord is the same dict the persistence
# layer writes; we reuse the exact field names so a peer's persisted record
# round-trips through the wire without translation.


def membership_to_dict(m: MembershipRecord) -> dict:
	d = {
		"host_id": m.host_id,
		"kind": m.kind.value,
		"state": m.state.value,
		"endpoint": m.endpoint,
		"wg_public_key": m.wg_public_key,
		"mesh_address": m.mesh_address,
		"generation": m.generation,
	}
	if m.signing_public_key:
		# Stage 5 — carries the origin's ed25519 signing pubkey so peers can
		# verify subsequent records' signatures against it. Absent on records
		# produced before Stage 5 (treated as "no signature required" by the
		# verifier; the §19.1 wg-transport binding remains in force).
		d["signing_public_key"] = m.signing_public_key
	return d


def membership_from_dict(d: dict) -> MembershipRecord:
	from .records import MembershipKind, MemberState

	return MembershipRecord(
		host_id=d["host_id"],
		kind=MembershipKind(d["kind"]),
		state=MemberState(d["state"]),
		endpoint=d["endpoint"],
		wg_public_key=d["wg_public_key"],
		mesh_address=d["mesh_address"],
		generation=int(d["generation"]),
		signing_public_key=d.get("signing_public_key", ""),
	)


def ownership_to_dict(a: OwnershipAdvertisement) -> dict:
	# frozenset -> sorted list so the bytes are canonical (the §16.2 render's
	# byte-compare depends on this when an advertisement is sent over the wire
	# and back). The signature rides alongside if present (Stage 5).
	d = {"origin": a.origin, "generation": a.generation, "owned": sorted(a.owned)}
	if a.signature:
		d["signature"] = a.signature
	return d


def ownership_from_dict(d: dict) -> OwnershipAdvertisement:
	return OwnershipAdvertisement(
		origin=d["origin"],
		generation=int(d["generation"]),
		owned=frozenset(d["owned"]),
		signature=d.get("signature", ""),
	)


# A small dispatch table the Gossip handler uses to decode piggybacked records
# of either kind. Each entry maps a wire type tag to (decoder, the record class).
# We tag piggyback entries with a short "k" field so a single piggyback list
# can mix MembershipRecords and OwnershipAdvertisements without a wrapper dict
# per entry — keeps the datagram compact.
RECORD_DECODERS: dict[str, Callable[[dict], Any]] = {
	"m": membership_from_dict,  # MembershipRecord
	"o": ownership_from_dict,  # OwnershipAdvertisement
}


def encode_record(record: MembershipRecord | OwnershipAdvertisement) -> dict:
	"""Tag a piggybacked record with its kind so the receiver dispatches to the
	right decoder. MembershipRecord → `{"k": "m", "v": <dict>}`;
	OwnershipAdvertisement → `{"k": "o", "v": <dict>}`. The `signature` field
	(if present on the dict) is preserved through the encode — Stage 5 wires
	it via `sign_outgoing`."""
	if isinstance(record, MembershipRecord):
		return {"k": "m", "v": membership_to_dict(record)}
	if isinstance(record, OwnershipAdvertisement):
		return {"k": "o", "v": ownership_to_dict(record)}
	raise TypeError(f"unknown record type: {type(record).__name__}")


def decode_record(tagged: dict) -> MembershipRecord | OwnershipAdvertisement:
	"""The inverse of `encode_record`. Raises `ValueError` on an unknown `k` so
	a future wire format change surfaces loud rather than silently dropping
	records the receiver doesn't recognize.

	Stage 5: the wire dict's `signature` field, if present, is NOT injected
	back into the record (the record is frozen + slots — can't carry ad-hoc
	attrs). The caller retrieves the wire signature via `wire_signature(tagged)`
	alongside this decode, threads it into the verifier via the daemon's
	`_incoming_wire_sigs` side-channel keyed by the record object's `id()`.
	Keeping the field off the dataclass also means the §13.3 dedupe cache +
	§16.2 render stay byte-equivalent across signed/unsigned records.
	"""
	kind = tagged.get("k")
	value = tagged.get("v")
	decoder = RECORD_DECODERS.get(kind)
	if decoder is None:
		raise ValueError(f"unknown record tag: {kind!r}")
	return decoder(value)


def wire_signature(tagged: dict) -> str | None:
	"""Return the wire signature from a tagged `{"k": .., "v": <dict>}` record,
	or None if the dict doesn't carry one (the pre-Stage-5 / unsigned path).
	The caller (gossip / antientropy apply path) passes this into the
	daemon's `_incoming_wire_sigs[id(record)]` side-channel before invoking
	`_apply_record`, so the verifier can read it."""
	value = tagged.get("v")
	if not isinstance(value, dict):
		return None
	return value.get("signature")


def attach_signature(tagged: dict, priv_b64: str) -> dict:
	"""Stage 5 — sign a tagged `{"k": "m"|"o", "v": <dict>}` record with the
	origin's ed25519 private key, attaching the `signature` field on the
	inner `v` dict. Returns the same `tagged` dict, mutated in place. A no-op
	if `priv_b64` is empty (the in-test / pre-Stage-5 path — records go out
	unsigned, and the verifier accepts unsigned)."""
	if not priv_b64:
		return tagged
	from . import signing

	kind = tagged.get("k")
	inner = tagged.get("v")
	if not isinstance(inner, dict):
		return tagged
	sig_kind = "membership" if kind == "m" else "ownership"
	inner["signature"] = signing.sign(inner, priv_b64, kind=sig_kind)
	return tagged


# --- concrete message payloads ----------------------------------------------


def gossip_payload(
	records: list[MembershipRecord | OwnershipAdvertisement],
) -> list[dict]:
	"""Build the `payload` for a `TYPE_GOSSIP` message: a tagged piggyback list.
	The Gossip handler trims this list to fit `MAX_DATAGRAM_BYTES` before send.

	Stage 5 signatures are attached by the SENDER (the daemon) at the
	`gossip_round` / `cold_join` / `anti_entropy_round` callsite, NOT here —
	only records whose origin is the sender should be signed by the sender; the
	sender has the context to decide. This pure wire-layer helper is only the
	tag+dict encoder.
	"""
	return [encode_record(r) for r in records]


def sign_records_if_owned(
	tagged_records: list[dict],
	own_signing_priv_b64: str,
	own_host_id: str,
) -> list[dict]:
	"""Stage 5 — sign each tagged record whose origin == `own_host_id` with the
	daemon's own ed25519 private key. Relay forwards records from other origins
	unchanged (they carry their original signer's signature already, OR are
	unsigned in a pre-Stage-5 mixed-version cluster — both are accepted by the
	verifier's "no sig → transport-binding fallback"). Mutates the tagged dicts
	in place. Returns the same list."""
	if not own_signing_priv_b64:
		return tagged_records
	for tagged in tagged_records:
		kind = tagged.get("k")
		inner = tagged.get("v")
		if not isinstance(inner, dict):
			continue
		# Only sign OUR records. MembershipRecord's origin is `v.host_id`;
		# OwnershipAdvertisement's origin is `v.origin`.
		origin = inner.get("host_id") if kind == "m" else inner.get("origin")
		if origin != own_host_id:
			continue
		attach_signature(tagged, own_signing_priv_b64)
	return tagged_records


def parse_gossip_payload(
	payload: Any,
	sigs_target: dict | None = None,
) -> list[MembershipRecord | OwnershipAdvertisement]:
	"""Decode a `TYPE_GOSSIP` payload into a list of records. Tolerant of an
	absent/empty payload (a heartbeat gossip with no piggyback). Stage 5: if
	`sigs_target` is given, populates it with `id(record) -> wire_sig` for
	each decoded record whose wire dict carried a signature; the caller
	attaches `sigs_target` onto the daemon as `_incoming_wire_sigs` before
	calling `_apply_record` so the verifier can read the wire signatures
	without polluting the frozen `slots=True` record classes with ad-hoc
	attributes."""
	if not payload:
		return []
	if not isinstance(payload, list):
		raise ValueError("gossip payload is not a list")
	records: list[MembershipRecord | OwnershipAdvertisement] = []
	for entry in payload:
		rec = decode_record(entry)
		records.append(rec)
		if sigs_target is not None:
			sig = wire_signature(entry)
			if sig is not None:
				sigs_target[id(rec)] = sig
	return records


def membership_advert_payload(record: MembershipRecord) -> dict:
	"""The payload of a `TYPE_MEMBERSHIP_ADVERT` (the cold-join unicast, §9.1):
	just the one Membership Record the newcomer is announcing."""
	return membership_to_dict(record)


def parse_membership_advert_payload(payload: Any) -> MembershipRecord:
	"""Decode a membership_advert payload — a single MembershipRecord dict."""
	if not isinstance(payload, dict):
		raise ValueError("membership_advert payload is not a dict")
	return membership_from_dict(payload)


# --- SWIM probe payloads (spec §14.2, Stage 4) ------------------------------
#
# A `Ping` carries a `nonce` so the corresponding `Ack` is matched to the
# originating probe round and a relayed indirect `Ack` doesn't spoof an
# unrelated probe as successful. The nonce is a 64-bit unsigned int, random
# per direct-probe attempt; cheaper and simpler than a hash (we have no
# valuable payload to protect, just a successful ping correlation).


def ping_payload(nonce: int, target_host_id: str) -> dict:
	"""A direct ping to `target_host_id` (over the plain-UDP ANCP socket,
	unicast). The peer responds with `ack_payload(nonce, target_host_id)` to
	confirm liveness."""
	return {"nonce": int(nonce), "target": target_host_id}


def ack_payload(nonce: int, target_host_id: str) -> dict:
	"""The reply to a `ping`. Same `nonce` + `target` — the prober matches the
	ack to a pending probe by (nonce, target)."""
	return {"nonce": int(nonce), "target": target_host_id}


def indirect_ping_payload(nonce: int, target_host_id: str, requester_host_id: str) -> dict:
	"""A request to a RELAY peer to forward a `ping` to `target_host_id` on our
	behalf (§14.2 step 3 — direct ping failed; try K relays in case the
	prober ↔ target path is one-way partitioned). The relay sends a `ping` to
	the target and forwards the ack to the original requester."""
	return {"nonce": int(nonce), "target": target_host_id, "requester": requester_host_id}


def parse_ping_payload(payload: Any) -> tuple[int, str]:
	if not isinstance(payload, dict):
		raise ValueError("ping payload is not a dict")
	return int(payload["nonce"]), str(payload["target"])


def parse_ack_payload(payload: Any) -> tuple[int, str]:
	return parse_ping_payload(payload)  # same shape


def parse_indirect_ping_payload(payload: Any) -> tuple[int, str, str]:
	if not isinstance(payload, dict):
		raise ValueError("indirect_ping payload is not a dict")
	return (
		int(payload["nonce"]),
		str(payload["target"]),
		str(payload["requester"]),
	)


__all__ = [
	"MAX_DATAGRAM_BYTES",
	"TYPE_ACK",
	"TYPE_ANTI_ENTROPY_REQ",
	"TYPE_ANTI_ENTROPY_RESP",
	"TYPE_GOSSIP",
	"TYPE_INDIRECT_PING",
	"TYPE_MEMBERSHIP_ADVERT",
	"TYPE_PING",
	"DatagramTooLarge",
	"Message",
	"ack_payload",
	"attach_signature",
	"decode_record",
	"encode_record",
	"from_bytes",
	"gossip_payload",
	"indirect_ping_payload",
	"membership_advert_payload",
	"membership_from_dict",
	"membership_to_dict",
	"ownership_from_dict",
	"ownership_to_dict",
	"parse_ack_payload",
	"parse_gossip_payload",
	"parse_indirect_ping_payload",
	"parse_membership_advert_payload",
	"parse_ping_payload",
	"ping_payload",
	"sign_records_if_owned",
]
