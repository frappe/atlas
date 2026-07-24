"""Unit tests for Stage 5 — ed25519 record signatures (spec §19.3) + the
conflict event hook (§7.3 / §18.2) + the metrics counter (§20.2).

`cryptography` is already a Frappe controller-side dep; we added it to the host
manifests too (`scripts/host-pyproject.toml` + `scripts/pyproject.toml`). Tests
run under `python3.14 -m unittest` with the bench env that already has it
installed — no new dep.
"""

import base64
import json
import tempfile
import unittest
from pathlib import Path

from atlas.networkd.config import Config
from atlas.networkd.conflicts import (
	ConflictEvent,
	ConflictTracker,
	observe_with_origins,
)
from atlas.networkd.daemon import build_initial, default_envelope_verifier, default_signature_verifier
from atlas.networkd.gossip import GossipState, handle_message
from atlas.networkd.identity import HostIdentity
from atlas.networkd.keys import ensure_signing_keypair
from atlas.networkd.observe import Counter
from atlas.networkd.records import (
	MembershipKind,
	MembershipRecord,
	MemberState,
	OwnershipAdvertisement,
	OwnershipTable,
	effective_ownership,
	owning_advertisement,
)
from atlas.networkd.signing import (
	SignatureError,
	generate_keypair_raw,
	sign,
	sign_envelope,
	sign_introduction,
	verify,
	verify_envelope,
	verify_introduction,
)
from atlas.networkd.state import AppliedState
from atlas.networkd.wire import (
	TYPE_GOSSIP,
	Message,
	attach_signature,
	decode_record,
	encode_record,
	from_bytes,
	gossip_payload,
	sign_records_if_owned,
)

# --- signing primitives -----------------------------------------------------


class TestSigning(unittest.TestCase):
	def test_round_trip_member(self):
		priv_raw, pub_raw = generate_keypair_raw()
		priv_b64 = base64.b64encode(priv_raw).decode()
		pub_b64 = base64.b64encode(pub_raw).decode()
		body = {"host_id": "h1", "kind": "member", "generation": 1}
		sig = sign(body, priv_b64, kind="membership")
		verify({**body, "signature": sig}, pub_b64, kind="membership")

	def test_tampered_body_rejected(self):
		priv_raw, pub_raw = generate_keypair_raw()
		priv_b64 = base64.b64encode(priv_raw).decode()
		pub_b64 = base64.b64encode(pub_raw).decode()
		body = {"host_id": "h1", "kind": "member", "generation": 1}
		sig = sign(body, priv_b64, kind="membership")
		# Tamper: change the host_id after signing → verify must raise.
		tampered = {**body, "host_id": "h-IMPOSTOR", "signature": sig}
		with self.assertRaises(SignatureError):
			verify(tampered, pub_b64, kind="membership")

	def test_wrong_pubkey_rejected(self):
		priv_raw, _ = generate_keypair_raw()
		_, other_pub_raw = generate_keypair_raw()
		priv_b64 = base64.b64encode(priv_raw).decode()
		wrong_pub_b64 = base64.b64encode(other_pub_raw).decode()
		body = {"host_id": "h1", "kind": "member", "generation": 1}
		sig = sign(body, priv_b64, kind="membership")
		with self.assertRaises(SignatureError):
			verify({**body, "signature": sig}, wrong_pub_b64, kind="membership")

	def test_missing_signature_raises(self):
		body = {"host_id": "h1"}
		with self.assertRaises(SignatureError):
			verify(body, "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", kind="membership")

	def test_kind_domain_separation(self):
		# A signature over a body tagged "membership" can't verify the same
		# body tagged "ownership" — domain separation.
		priv_raw, pub_raw = generate_keypair_raw()
		priv_b64 = base64.b64encode(priv_raw).decode()
		pub_b64 = base64.b64encode(pub_raw).decode()
		body = {"origin": "h1", "generation": 1}
		sig = sign(body, priv_b64, kind="membership")
		with self.assertRaises(SignatureError):
			verify({**body, "signature": sig}, pub_b64, kind="ownership")


# --- key management --------------------------------------------------------


class TestEnsureSigningKeypair(unittest.TestCase):
	def test_generates_when_absent(self):
		with tempfile.TemporaryDirectory() as d:
			priv = Path(d) / "priv"
			pub = Path(d) / "pub"
			priv_b64, pub_b64 = ensure_signing_keypair(str(priv), signing_pub_path=str(pub))
			self.assertTrue(priv.exists())
			self.assertTrue(pub.exists())
			# Round-trip: sign + verify with the keys just written.
			body = {"v": "self-test", "host_id": "self"}
			sig = sign(body, priv_b64, kind="membership")
			verify({**body, "signature": sig}, pub_b64, kind="membership")
			import os

			self.assertEqual(os.stat(priv).st_mode & 0o777, 0o600)
			self.assertEqual(os.stat(pub).st_mode & 0o777, 0o644)

	def test_idempotent_when_pair_valid(self):
		with tempfile.TemporaryDirectory() as d:
			priv = Path(d) / "priv"
			pub = Path(d) / "pub"
			first = ensure_signing_keypair(str(priv), signing_pub_path=str(pub))
			second = ensure_signing_keypair(str(priv), signing_pub_path=str(pub))
			self.assertEqual(first, second)  # same keypair returned both times

	def test_regenerates_when_pair_mismatched(self):
		# A private key with a wrong public mate → detect (the self-test
		# verify inside `_existing_signing_pair_valid` fails) → regenerate.
		with tempfile.TemporaryDirectory() as d:
			priv = Path(d) / "priv"
			pub = Path(d) / "pub"
			ensure_signing_keypair(str(priv), signing_pub_path=str(pub))
			# Tamper with the public key file → mismatch on next check.
			_, other_pub_raw = generate_keypair_raw()
			other_pub_b64 = base64.b64encode(other_pub_raw).decode()
			pub.write_text(other_pub_b64 + "\n")
			_new_priv, new_pub = ensure_signing_keypair(str(priv), signing_pub_path=str(pub))
			# The public we just read must match the new keypair's public — i.e.
			# we regenerated both files together.
			self.assertEqual(new_pub, pub.read_text().strip())


# --- wire: sign_records_if_owned + attach_signature ----------------------


class TestWireSigning(unittest.TestCase):
	def _record_pair(self):
		priv_raw, pub_raw = generate_keypair_raw()
		return base64.b64encode(priv_raw).decode(), base64.b64encode(pub_raw).decode()

	def test_sign_records_if_owned_only_signs_own(self):
		# Two records in the piggyback: our own Membership + a relayed
		# Ownership from another origin. Only OUR record gets signed.
		priv_b64, pub_b64 = self._record_pair()
		own = MembershipRecord(
			host_id="h1",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::h1",
			wg_public_key="K",
			mesh_address="fdaa:0:0:h1::1",
			generation=1,
			signing_public_key=pub_b64,
		)
		relay = OwnershipAdvertisement(
			origin="h2",  # the relay's origin
			generation=4,
			owned=frozenset({"fdaa::2"}),
		)
		tagged = sign_records_if_owned(
			gossip_payload([own, relay]),
			priv_b64,
			own_host_id="h1",
		)
		# Our own record carries a signature; the relayed one doesn't.
		own_tagged = next(t for t in tagged if t["k"] == "m" and t["v"]["host_id"] == "h1")
		relay_tagged = next(t for t in tagged if t["k"] == "o" and t["v"]["origin"] == "h2")
		self.assertIn("signature", own_tagged["v"])
		self.assertNotIn("signature", relay_tagged["v"])
		# And the own signature verifies.
		verify(own_tagged["v"], pub_b64, kind="membership")

	def test_sign_records_if_owned_noop_when_no_key(self):
		# A daemon in-test (empty signing key) emits NO signatures — pre-Stage-5
		# behavior preserved.
		own = MembershipRecord(
			host_id="h1",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::h1",
			wg_public_key="K",
			mesh_address="fdaa:0:0:h1::1",
			generation=1,
		)
		tagged = sign_records_if_owned(gossip_payload([own]), "", own_host_id="h1")
		self.assertNotIn("signature", tagged[0]["v"])

	def test_decode_record_does_not_carry_wire_sig(self):
		# Stage 5 — the wire signature stays in the wire dict (not on the
		# frozen-slots record); the caller reads it via `wire.wire_signature`
		# and threads it through the daemon's side-channel. Assert that
		# `decode_record` produces the record unchanged, and `wire_signature`
		# returns the signature separately.
		priv_raw, pub_raw = generate_keypair_raw()
		priv_b64 = base64.b64encode(priv_raw).decode()
		pub_b64 = base64.b64encode(pub_raw).decode()
		own = MembershipRecord(
			host_id="h1",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::h1",
			wg_public_key="K",
			mesh_address="fdaa:0:0:h1::1",
			generation=1,
			signing_public_key=pub_b64,
		)
		tagged = sign_records_if_owned(gossip_payload([own]), priv_b64, own_host_id="h1")[0]
		decoded = decode_record(tagged)
		self.assertFalse(hasattr(decoded, "_wire_sig"))
		from atlas.networkd.wire import wire_signature

		self.assertEqual(wire_signature(tagged), tagged["v"]["signature"])


# --- default_signature_verifier (the production verify path) ---------------


class _FakeDaemon:
	"""A tiny stand-in for `Daemon` exposes the two fields the verifier needs
	(`state.membership` for ownership-pubkey lookup) without dragging the full
	dataclass in. The gossip apply path uses `daemon.state.membership` to look
	up the origin's Membership Record; we mirror that exactly."""

	def __init__(self, state: AppliedState):
		self.state = state
		self.metrics = Counter()


class TestDefaultVerifier(unittest.TestCase):
	def test_signed_membership_record_verifies(self):
		priv_raw, pub_raw = generate_keypair_raw()
		priv_b64 = base64.b64encode(priv_raw).decode()
		pub_b64 = base64.b64encode(pub_raw).decode()
		state = AppliedState()
		own = MembershipRecord(
			host_id="h1",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::h1",
			wg_public_key="K",
			mesh_address="fdaa:0:0:h1::1",
			generation=1,
			signing_public_key=pub_b64,
		)
		d = _FakeDaemon(state)
		tagged = sign_records_if_owned(gossip_payload([own]), priv_b64, own_host_id="h1")[0]
		decoded = decode_record(tagged)
		# Populate the side-channel the way `_handle_gossip` does.
		from atlas.networkd.wire import wire_signature

		d._incoming_wire_sigs = {id(decoded): wire_signature(tagged)}
		default_signature_verifier(decoded, d)  # should not raise

	def test_unsigned_membership_record_rejected(self):
		# An unsigned record WITH a signing_public_key set: the verifier MUST
		# reject — a peer that advertises a signing key must sign every record.
		state = AppliedState()
		own = MembershipRecord(
			host_id="h1",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::h1",
			wg_public_key="K",
			mesh_address="fdaa:0:0:h1::1",
			generation=1,
			signing_public_key="PUBKEY",
		)
		d = _FakeDaemon(state)
		with self.assertRaises(SignatureError):
			default_signature_verifier(own, d)

	def test_unsigned_downgrade_rejected_when_stored_has_key(self):
		# If the stored record already has a non-empty signing_public_key, an
		# incoming unsigned record (signing_public_key="") at a higher generation
		# must be rejected — it's a downgrade attempt that would erase the
		# signing key and let the attacker forge unsigned ownership claims.
		state = AppliedState()
		state.apply_membership(
			MembershipRecord(
				host_id="h1",
				kind=MembershipKind.MEMBER,
				state=MemberState.ALIVE,
				endpoint="2001:db9::h1",
				wg_public_key="K",
				mesh_address="fdaa:0:0:h1::1",
				generation=5,
				signing_public_key="EXISTING_PUBKEY",
			)
		)
		d = _FakeDaemon(state)
		incoming = MembershipRecord(
			host_id="h1",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::h1",
			wg_public_key="K",
			mesh_address="fdaa:0:0:h1::1",
			generation=6,  # higher gen — would replace without the guard
			signing_public_key="",
		)
		with self.assertRaises(SignatureError):
			default_signature_verifier(incoming, d)

	def test_forged_signature_drops_via_apply(self):
		# A record's `signing_public_key` claims a different origin's pubkey
		# (a forgery — would let an attacker route records through someone
		# else's signing-key slot). The verifier must reject. The wire
		# signature was computed with the legitimate priv key but the record
		# claims the OTHER pubkey — the verify call fails.
		priv_raw, pub_raw = generate_keypair_raw()
		priv_b64 = base64.b64encode(priv_raw).decode()
		_pub_b64 = base64.b64encode(pub_raw).decode()
		_, other_pub_raw = generate_keypair_raw()
		other_pub_b64 = base64.b64encode(other_pub_raw).decode()
		state = AppliedState()
		body = MembershipRecord(
			host_id="h1",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::h1",
			wg_public_key="K",
			mesh_address="fdaa:0:0:h1::1",
			generation=2,
			signing_public_key=other_pub_b64,  # mismatch — verify must fail
		)
		tagged = sign_records_if_owned(gossip_payload([body]), priv_b64, own_host_id="h1")[0]
		decoded = decode_record(tagged)
		d = _FakeDaemon(state)
		d.metrics = Counter()
		# Populate the side-channel the way `_handle_gossip` does.
		from atlas.networkd.wire import wire_signature

		d._incoming_wire_sigs = {id(decoded): wire_signature(tagged)}
		with self.assertRaises(SignatureError):
			default_signature_verifier(decoded, d)

	def test_key_hijack_rejected_when_stored_key_differs(self):
		# Attacker creates its own keypair, publishes a MembershipRecord for
		# h1 with the attacker's key, self-signed. The stored record has h1's
		# real signing key — the verifier must check against the STORED key,
		# so the attacker's self-signed record is rejected. Verifier should
		# reject even though the incoming record's self-signature is valid.
		_real_priv, real_pub = generate_keypair_raw()
		real_pub_b64 = base64.b64encode(real_pub).decode()
		att_priv, att_pub = generate_keypair_raw()
		att_pub_b64 = base64.b64encode(att_pub).decode()
		state = AppliedState()
		state.apply_membership(
			MembershipRecord(
				host_id="h1",
				kind=MembershipKind.MEMBER,
				state=MemberState.ALIVE,
				endpoint="2001:db9::h1",
				wg_public_key="K",
				mesh_address="fdaa:0:0:h1::1",
				generation=5,
				signing_public_key=real_pub_b64,
			)
		)
		body = MembershipRecord(
			host_id="h1",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::h1",
			wg_public_key="K",
			mesh_address="fdaa:0:0:h1::1",
			generation=6,
			signing_public_key=att_pub_b64,
		)
		att_priv_b64 = base64.b64encode(att_priv).decode()
		tagged = sign_records_if_owned(gossip_payload([body]), att_priv_b64, own_host_id="h1")[0]
		decoded = decode_record(tagged)
		d = _FakeDaemon(state)
		d.metrics = Counter()
		from atlas.networkd.wire import wire_signature

		d._incoming_wire_sigs = {id(decoded): wire_signature(tagged)}
		with self.assertRaises(SignatureError):
			default_signature_verifier(decoded, d)


# --- M1: unsigned OwnershipAdvertisement from an unknown origin is DEFERRED --


class TestOwnershipDeferredWithoutMembership(unittest.TestCase):
	"""Spec §19.3 — an OwnershipAdvertisement from an origin the receiver has no
	MembershipRecord (hence no trusted signing pubkey) for MUST NOT be applied.
	It is deferred: dropped + counted, the ownership gen-vector is NOT advanced,
	so anti-entropy re-pulls it once the origin's MembershipRecord arrives."""

	def _member_with_key(self, host_id, gen, signing_pub_b64):
		return MembershipRecord(
			host_id=host_id,
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint=f"2001:db9::{host_id}",
			wg_public_key="K",
			mesh_address=f"fdaa:0:0:{host_id}::1",
			generation=gen,
			signing_public_key=signing_pub_b64,
		)

	def _daemon(self, state):
		from atlas.networkd.daemon import default_signature_verifier

		d = _FakeDaemon(state)
		d.signature_verifier = default_signature_verifier
		d.signing_pubkey_cache = {}
		d.failure_tracker = None
		return d

	def test_unsigned_ad_from_unknown_origin_not_applied(self):
		from atlas.networkd.antientropy import build_vector
		from atlas.networkd.gossip import _apply_record

		state = AppliedState()
		d = self._daemon(state)
		# An OwnershipAdvertisement whose origin has no MembershipRecord yet.
		ad = owning_advertisement("h2", 3, ("fdaa::99",))
		changed = _apply_record(ad, d)
		# NOT applied — dropped + counted, effective table stays empty.
		self.assertFalse(changed)
		self.assertNotIn("h2", state.ownership)
		self.assertNotIn("fdaa::99", effective_ownership(state.ownership).owner_of)
		self.assertEqual(d.metrics.snapshot().get("signature_failed"), 1)
		# The ownership gen-vector is NOT advanced for h2 — so anti-entropy will
		# re-pull the record (theirs=None in `_missing_for_requester`) once h2's
		# membership arrives. If it HAD been applied, vector_o["h2"]=3 would stop
		# the re-pull of the same-generation authentic record.
		self.assertNotIn("h2", build_vector(state)["vector_o"])

	def test_bogus_unsigned_ad_cannot_manufacture_conflict(self):
		from atlas.networkd.gossip import _apply_record

		# A real owner h1 (with membership + a signed ad) owns fdaa::1. A relay
		# injects an UNSIGNED ad from unknown origin h-phantom also claiming
		# fdaa::1 — that must NOT create a §7.3 conflict entry.
		priv_raw, pub_raw = generate_keypair_raw()
		priv_b64 = base64.b64encode(priv_raw).decode()
		pub_b64 = base64.b64encode(pub_raw).decode()
		state = AppliedState()
		state.apply_membership(self._member_with_key("h1", 1, pub_b64))
		d = self._daemon(state)
		d.signing_pubkey_cache = {"h1": pub_b64}
		# h1's own signed ad applies.
		h1_ad = owning_advertisement("h1", 1, ("fdaa::1",))
		tagged = sign_records_if_owned(gossip_payload([h1_ad]), priv_b64, own_host_id="h1")[0]
		decoded = decode_record(tagged)
		from atlas.networkd.wire import wire_signature

		d._incoming_wire_sigs = {id(decoded): wire_signature(tagged)}
		self.assertTrue(_apply_record(decoded, d))
		# The phantom unsigned ad for the same /128 is dropped, not applied.
		phantom = owning_advertisement("h-phantom", 9, ("fdaa::1",))
		self.assertFalse(_apply_record(phantom, d))
		self.assertNotIn("h-phantom", state.ownership)
		# No conflict — only h1 owns fdaa::1 in the effective table.
		table = effective_ownership(state.ownership)
		self.assertEqual(table.owner_of.get("fdaa::1"), "h1")
		self.assertEqual(table.conflicts, frozenset())

	def test_ad_applies_after_membership_arrives(self):
		from atlas.networkd.gossip import _apply_record
		from atlas.networkd.wire import wire_signature

		priv_raw, pub_raw = generate_keypair_raw()
		priv_b64 = base64.b64encode(priv_raw).decode()
		pub_b64 = base64.b64encode(pub_raw).decode()
		state = AppliedState()
		d = self._daemon(state)
		# First delivery: the signed ad arrives BEFORE h2's membership → deferred.
		ad = owning_advertisement("h2", 3, ("fdaa::7",))
		ad_tagged = sign_records_if_owned(gossip_payload([ad]), priv_b64, own_host_id="h2")[0]
		ad_decoded = decode_record(ad_tagged)
		d._incoming_wire_sigs = {id(ad_decoded): wire_signature(ad_tagged)}
		self.assertFalse(_apply_record(ad_decoded, d))
		self.assertNotIn("h2", state.ownership)
		# Now h2's MembershipRecord (carrying its signing key) arrives + applies.
		mem = self._member_with_key("h2", 1, pub_b64)
		mem_tagged = sign_records_if_owned(gossip_payload([mem]), priv_b64, own_host_id="h2")[0]
		mem_decoded = decode_record(mem_tagged)
		d._incoming_wire_sigs = {id(mem_decoded): wire_signature(mem_tagged)}
		self.assertTrue(_apply_record(mem_decoded, d))
		# Re-delivery of the signed ad (as anti-entropy would re-pull) now applies.
		ad_decoded2 = decode_record(ad_tagged)
		d._incoming_wire_sigs = {id(ad_decoded2): wire_signature(ad_tagged)}
		self.assertTrue(_apply_record(ad_decoded2, d))
		self.assertEqual(state.ownership["h2"].owned, frozenset({"fdaa::7"}))


# --- conflict event hook (§7.3 / §18.2) ----------------------------------


class TestConflictEvents(unittest.TestCase):
	def test_start_and_end_events_fire(self):
		# Two hosts each advertise the SAME /128 → conflict → START event.
		# One drops the /128 from its next advertisement → conflict clears →
		# END event.
		evs: list[ConflictEvent] = []
		clock = [0.0]
		tracker = ConflictTracker(now_fn=lambda: clock[0])
		tracker.subscribe(evs.append)
		latest = {
			"h1": owning_advertisement("h1", 1, ("fdaa::1",)),
			"h2": owning_advertisement("h2", 1, ("fdaa::1",)),
		}
		table = effective_ownership(latest)
		emitted = observe_with_origins(tracker, table, latest)
		self.assertEqual(len(emitted), 1)
		self.assertEqual(emitted[0].kind, "start")
		self.assertEqual(emitted[0].private_ip, "fdaa::1")
		self.assertEqual(emitted[0].origins, frozenset({"h1", "h2"}))
		# h2 then withdraws → no conflict.
		clock[0] = 5.0
		latest2 = {
			"h1": owning_advertisement("h1", 2, ("fdaa::1",)),
			"h2": owning_advertisement("h2", 2, ()),
		}
		table2 = effective_ownership(latest2)
		emitted2 = observe_with_origins(tracker, table2, latest2)
		self.assertEqual(len(emitted2), 1)
		self.assertEqual(emitted2[0].kind, "end")
		self.assertEqual(emitted2[0].private_ip, "fdaa::1")

	def test_distinct_ips_do_not_fire(self):
		# Two origins, two different /128s — no conflict, no events.
		evs: list[ConflictEvent] = []
		tracker = ConflictTracker()
		tracker.subscribe(evs.append)
		latest = {
			"h1": owning_advertisement("h1", 1, ("fdaa::1",)),
			"h2": owning_advertisement("h2", 1, ("fdaa::2",)),
		}
		table = effective_ownership(latest)
		emitted = observe_with_origins(tracker, table, latest)
		self.assertEqual(emitted, [])

	def test_log_file_appends_one_json_line_per_event(self):
		# The file sink at /var/lib/atlas-networkd/conflicts.jsonl is overridden
		# via the ConflictTracker's _log_path; one JSON line per event.
		with tempfile.TemporaryDirectory() as d:
			log_path = Path(d) / "conflicts.jsonl"
			tracker = ConflictTracker(now_fn=lambda: 12345.0)
			tracker._log_path = str(log_path)
			latest = {
				"h1": owning_advertisement("h1", 1, ("fdaa::1",)),
				"h2": owning_advertisement("h2", 1, ("fdaa::1",)),
			}
			observe_with_origins(tracker, effective_ownership(latest), latest)
			lines = log_path.read_text().splitlines()
			self.assertEqual(len(lines), 1)
			doc = json.loads(lines[0])
			self.assertEqual(doc["kind"], "start")
			self.assertEqual(doc["private_ip"], "fdaa::1")
			self.assertEqual(sorted(doc["origins"]), ["h1", "h2"])
			self.assertEqual(doc["at"], 12345.0)


# --- metrics counter ------------------------------------------------------


class TestCounter(unittest.TestCase):
	def test_incr_and_snapshot(self):
		from atlas.networkd.observe import Counter

		c = Counter()
		c.incr("signature_failed")
		c.incr("signature_failed")
		c.incr("apply_count")
		snap = c.snapshot()
		self.assertEqual(snap["signature_failed"], 2)
		self.assertEqual(snap["apply_count"], 1)

	def test_incr_default_by_one(self):
		from atlas.networkd.observe import Counter

		c = Counter()
		c.incr("x")
		self.assertEqual(c.snapshot()["x"], 1)
		c.incr("x", by=5)
		self.assertEqual(c.snapshot()["x"], 6)


# --- Stage 5+ envelope + introduction signatures (§19.1, §19.5) ------------


class TestEnvelopeSignatures(unittest.TestCase):
	"""Round-trip the `sign_envelope` / `verify_envelope` primitives (spec
	§19.1) — every ANCP `Message` is whole-envelope-signed by the sender's
	ed25519 key."""

	def test_sign_envelope_round_trip(self):
		priv_raw, pub_raw = generate_keypair_raw()
		priv_b64 = base64.b64encode(priv_raw).decode()
		pub_b64 = base64.b64encode(pub_raw).decode()
		body = {"type": "gossip", "sender": "h1", "signing_public_key": pub_b64, "payload": ["x"]}
		sig = sign_envelope(body, priv_b64)
		verify_envelope(body, sig, pub_b64)

	def test_tampered_envelope_body_fails_verify(self):
		priv_raw, pub_raw = generate_keypair_raw()
		priv_b64 = base64.b64encode(priv_raw).decode()
		pub_b64 = base64.b64encode(pub_raw).decode()
		body = {"type": "gossip", "sender": "h1", "signing_public_key": pub_b64, "payload": ["x"]}
		sig = sign_envelope(body, priv_b64)
		# Mutate the payload — must invalidate the signature.
		tampered = {**body, "payload": ["y"]}
		with self.assertRaises(SignatureError):
			verify_envelope(tampered, sig, pub_b64)

	def test_verify_envelope_rejects_wrong_key(self):
		attacker_priv_raw, _ = generate_keypair_raw()
		attacker_priv_b64 = base64.b64encode(attacker_priv_raw).decode()
		legit_pub_raw = generate_keypair_raw()[1]
		legit_pub_b64 = base64.b64encode(legit_pub_raw).decode()
		body = {"type": "gossip", "sender": "h1", "signing_public_key": legit_pub_b64}
		sig = sign_envelope(body, attacker_priv_b64)  # signed by attacker
		with self.assertRaises(SignatureError):
			verify_envelope(body, sig, legit_pub_b64)

	def test_verify_envelope_rejects_empty_signature(self):
		pub_b64 = base64.b64encode(generate_keypair_raw()[1]).decode()
		body = {"type": "gossip", "sender": "h1", "signing_public_key": pub_b64}
		with self.assertRaises(SignatureError):
			verify_envelope(body, "", pub_b64)

	def test_domain_separation_blocked_across_kinds(self):
		"""An envelope signature must not verify as a membership-record
		signature, and vice versa — the `_kind` tag is the domain separator."""
		priv_raw, pub_raw = generate_keypair_raw()
		priv_b64 = base64.b64encode(priv_raw).decode()
		pub_b64 = base64.b64encode(pub_raw).decode()
		body = {"type": "gossip", "sender": "h1", "signing_public_key": pub_b64, "payload": []}
		sig = sign_envelope(body, priv_b64)
		# Try to reuse it as a membership-record signature.
		d = {"host_id": "h1", "kind": "member"}
		with self.assertRaises(SignatureError):
			verify({**d, "signature": sig}, pub_b64, kind="membership")


class TestIntroductionSignatures(unittest.TestCase):
	"""Round-trip the operator-signed newcomer introduction (spec §19.5) —
	the operator's provision key signs the `(host_id, signing_public_key,
	generation)` binding."""

	def test_sign_introduction_round_trip(self):
		op_priv_raw, op_pub_raw = generate_keypair_raw()
		op_priv_b64 = base64.b64encode(op_priv_raw).decode()
		op_pub_b64 = base64.b64encode(op_pub_raw).decode()
		body = {"host_id": "Q", "signing_public_key": "KQ", "generation": 1}
		sig = sign_introduction(body, op_priv_b64)
		verify_introduction(body, sig, op_pub_b64)

	def test_tampered_introduction_body_fails_verify(self):
		op_priv_raw, op_pub_raw = generate_keypair_raw()
		op_priv_b64 = base64.b64encode(op_priv_raw).decode()
		op_pub_b64 = base64.b64encode(op_pub_raw).decode()
		body = {"host_id": "Q", "signing_public_key": "KQ", "generation": 1}
		sig = sign_introduction(body, op_priv_b64)
		tampered = {**body, "host_id": "attacker"}
		with self.assertRaises(SignatureError):
			verify_introduction(tampered, sig, op_pub_b64)

	def test_verify_introduction_rejects_wrong_operator(self):
		op_priv_raw, _op_pub_raw = generate_keypair_raw()
		op_priv_b64 = base64.b64encode(op_priv_raw).decode()
		other_pub_b64 = base64.b64encode(generate_keypair_raw()[1]).decode()
		body = {"host_id": "Q", "signing_public_key": "KQ", "generation": 1}
		sig = sign_introduction(body, op_priv_b64)
		with self.assertRaises(SignatureError):
			verify_introduction(body, sig, other_pub_b64)

	def test_verify_introduction_rejects_empty_sig(self):
		op_pub_b64 = base64.b64encode(generate_keypair_raw()[1]).decode()
		body = {"host_id": "Q", "signing_public_key": "KQ", "generation": 1}
		with self.assertRaises(SignatureError):
			verify_introduction(body, "", op_pub_b64)


# --- default_envelope_verifier (the production recv-path verifier) ----------


class _StateStub:
	"""Minimal stand-in for `AppliedState` — the envelope verifier's key-
	rotation fallback (§19.3) consults `state.membership` for a known sender's
	established signing_public_key. Tests that exercise the mismatch path
	treat the membership table as empty: the cached-key rotation path
	doesn't apply, and the self-asserted-key verify path runs (and rejects)."""

	def __init__(self, membership: dict | None = None):
		self.membership = dict(membership or {})


class _EnvelopeDaemon:
	"""Minimal stand-in for `Daemon` — exposes the four fields the envelope
	verifier reads: `signing_pubkey_cache`, `operator_public_key`, `metrics`,
	and `state.membership` (the §19.3 key-rotation fallback)."""

	def __init__(
		self,
		operator_public_key: str = "",
		cache: dict[str, str] | None = None,
		membership: dict | None = None,
	):
		self.signing_pubkey_cache = dict(cache or {})
		self.operator_public_key = operator_public_key
		self.metrics = Counter()
		self.state = _StateStub(membership=membership)


class TestDefaultEnvelopeVerifier(unittest.TestCase):
	def test_cached_sender_with_valid_envelope_verifies(self):
		"""A datagram from a seeded host, envelope-signed with the cached key,
		verifies and is left through to the payload handler."""
		sender_priv_raw, sender_pub_raw = generate_keypair_raw()
		sender_priv_b64 = base64.b64encode(sender_priv_raw).decode()
		sender_pub_b64 = base64.b64encode(sender_pub_raw).decode()
		d = _EnvelopeDaemon(cache={"h1": sender_pub_b64})
		msg = Message(
			type="gossip",
			sender="h1",
			signing_public_key=sender_pub_b64,
			payload=["x"],
		)
		msg = from_bytes(msg.to_bytes(sender_priv_b64))
		# must not raise
		default_envelope_verifier(msg, d)
		# cache unchanged
		self.assertEqual(d.signing_pubkey_cache, {"h1": sender_pub_b64})

	def test_cached_sender_with_missing_signature_fails(self):
		"""A datagram from a seeded host that omits the envelope signature is
		rejected — no unsigned acceptance on a known sender."""
		sender_pub_b64 = base64.b64encode(generate_keypair_raw()[1]).decode()
		d = _EnvelopeDaemon(cache={"h1": sender_pub_b64})
		# Build the wire bytes without passing a priv → no `signature` field.
		msg = from_bytes(
			Message(type="gossip", sender="h1", signing_public_key=sender_pub_b64, payload=[]).to_bytes()
		)
		with self.assertRaises(SignatureError):
			default_envelope_verifier(msg, d)

	def test_cached_sender_with_self_asserted_key_mismatch_fails(self):
		"""A datagram from a seeded host whose `signing_public_key` differs
		from the cached one is rejected (rotation must come via a signed
		MembershipRecord from the established key, §19.3)."""
		sender_priv_raw, sender_pub_raw = generate_keypair_raw()
		sender_priv_b64 = base64.b64encode(sender_priv_raw).decode()
		sender_pub_b64 = base64.b64encode(sender_pub_raw).decode()
		other_pub_b64 = base64.b64encode(generate_keypair_raw()[1]).decode()
		d = _EnvelopeDaemon(cache={"h1": sender_pub_b64})
		# Sign with sender_priv but self-assert a DIFFERENT pubkey on the wire.
		msg = Message(
			type="gossip",
			sender="h1",
			signing_public_key=other_pub_b64,
			payload=[],
		)
		msg = from_bytes(msg.to_bytes(sender_priv_b64))
		with self.assertRaises(SignatureError):
			default_envelope_verifier(msg, d)

	def test_cached_sender_with_bad_key_downgrade_fails(self):
		"""A datagram from a seeded host that drops `signing_public_key` (the
		wire default "" — a downgrade to "unsigned") is rejected even if the
		envelope signature is well-formed against the cached key."""
		sender_priv_raw, sender_pub_raw = generate_keypair_raw()
		sender_priv_b64 = base64.b64encode(sender_priv_raw).decode()
		sender_pub_b64 = base64.b64encode(sender_pub_raw).decode()
		d = _EnvelopeDaemon(cache={"h1": sender_pub_b64})
		# Wire-rig: sign with the right key, but self-assert an empty pubkey.
		# Use the dataclass-without-pubkey path, then to_bytes(sender_priv) so
		# the signature is over the canonical body with signing_public_key="".
		msg = Message(type="gossip", sender="h1", signing_public_key="", payload=[])
		msg = from_bytes(msg.to_bytes(sender_priv_b64))
		with self.assertRaises(SignatureError):
			default_envelope_verifier(msg, d)

	def test_unknown_sender_without_introduction_sig_fails(self):
		"""First-contact datagrams from an unknown sender MUST carry an
		`introduction_signature` (§19.5) — without it, the receiver can't
		anchor the self-asserted key to the operator."""
		attacker_pub_b64 = base64.b64encode(generate_keypair_raw()[1]).decode()
		d = _EnvelopeDaemon(operator_public_key="op_pub")  # operator is set; if missing the test breaks
		msg = Message(type="membership_advert", sender="Q", signing_public_key=attacker_pub_b64, payload={})
		msg = from_bytes(msg.to_bytes())  # no signature, no introduction
		with self.assertRaises(SignatureError):
			default_envelope_verifier(msg, d)

	def test_unknown_sender_without_self_asserted_key_fails(self):
		"""First-contact without even a self-asserted pubkey is rejected
		outright — there's nothing to anchor against the operator cert."""
		d = _EnvelopeDaemon(operator_public_key="op_pub")
		msg = from_bytes(Message(type="membership_advert", sender="Q", payload={}).to_bytes())
		with self.assertRaises(SignatureError):
			default_envelope_verifier(msg, d)

	def test_unknown_sender_with_valid_introduction_TOFUs_key(self):
		"""First contact with a valid operator-signed introduction
		certificate: the self-asserted key is TOFU'd into the cache. Later
		datagrams from the same sender verify purely against the cache."""
		op_priv_raw, op_pub_raw = generate_keypair_raw()
		op_priv_b64 = base64.b64encode(op_priv_raw).decode()
		op_pub_b64 = base64.b64encode(op_pub_raw).decode()
		new_priv_raw, new_pub_raw = generate_keypair_raw()
		new_priv_b64 = base64.b64encode(new_priv_raw).decode()
		new_pub_b64 = base64.b64encode(new_pub_raw).decode()
		d = _EnvelopeDaemon(operator_public_key=op_pub_b64)
		# Build the introduction body (the wire dict inside payload).
		payload = {
			"host_id": "Q",
			"signing_public_key": new_pub_b64,
			"generation": 1,
		}
		intro_body = {
			"host_id": payload["host_id"],
			"signing_public_key": payload["signing_public_key"],
			"generation": payload["generation"],
		}
		intro_sig = sign_introduction(intro_body, op_priv_b64)
		msg = Message(
			type="membership_advert",
			sender="Q",
			signing_public_key=new_pub_b64,
			payload=payload,
			introduction_signature=intro_sig,
		)
		msg = from_bytes(msg.to_bytes(new_priv_b64))
		default_envelope_verifier(msg, d)
		# TOFU — the key is now anchored in the cache.
		self.assertEqual(d.signing_pubkey_cache.get("Q"), new_pub_b64)
		# A subsequent datagram (no introduction sig) verifies against the cache.
		follow_up = Message(
			type="gossip",
			sender="Q",
			signing_public_key=new_pub_b64,
			payload=[],
		)
		follow_up = from_bytes(follow_up.to_bytes(new_priv_b64))
		default_envelope_verifier(follow_up, d)

	def test_unknown_sender_with_bad_introduction_sig_fails(self):
		"""An introduction cert that does NOT verify against the operator
		pubkey is rejected — the self-asserted key is NOT TOFU'd."""
		op_pub_b64 = base64.b64encode(generate_keypair_raw()[1]).decode()
		other_priv_raw, other_pub_raw = generate_keypair_raw()
		other_priv_b64 = base64.b64encode(other_priv_raw).decode()
		other_pub_b64 = base64.b64encode(other_pub_raw).decode()
		d = _EnvelopeDaemon(operator_public_key=op_pub_b64)
		payload = {
			"host_id": "Q",
			"signing_public_key": other_pub_b64,
			"generation": 1,
		}
		intro_body = {
			"host_id": payload["host_id"],
			"signing_public_key": payload["signing_public_key"],
			"generation": payload["generation"],
		}
		not_op_priv_b64 = base64.b64encode(generate_keypair_raw()[0]).decode()
		intro_sig = sign_introduction(intro_body, not_op_priv_b64)  # not the operator's
		msg = Message(
			type="membership_advert",
			sender="Q",
			signing_public_key=other_pub_b64,
			payload=payload,
			introduction_signature=intro_sig,
		)
		msg = from_bytes(msg.to_bytes(other_priv_b64))
		with self.assertRaises(SignatureError):
			default_envelope_verifier(msg, d)
		# cache NOT updated
		self.assertNotIn("Q", d.signing_pubkey_cache)

	def test_unknown_sender_when_no_operator_pubkey_fails(self):
		"""A daemon with no operator pubkey seeded (misconfiguration / not
		yet provisioned) cannot onboard any newcomer — the introduction has
		no key to verify against."""
		new_priv_raw, new_pub_raw = generate_keypair_raw()
		new_priv_b64 = base64.b64encode(new_priv_raw).decode()
		new_pub_b64 = base64.b64encode(new_pub_raw).decode()
		d = _EnvelopeDaemon(operator_public_key="")  # misconfigured
		payload = {"host_id": "Q", "signing_public_key": new_pub_b64, "generation": 1}
		intro_body = {
			"host_id": payload["host_id"],
			"signing_public_key": payload["signing_public_key"],
			"generation": payload["generation"],
		}
		intro_sig = sign_introduction(
			intro_body, new_priv_b64
		)  # not even signed by operator, but irrelevant: nobody can verify
		msg = Message(
			type="membership_advert",
			sender="Q",
			signing_public_key=new_pub_b64,
			payload=payload,
			introduction_signature=intro_sig,
		)
		msg = from_bytes(msg.to_bytes(new_priv_b64))
		with self.assertRaises(SignatureError):
			default_envelope_verifier(msg, d)


# --- M6: TOFU-learned signing keys survive a restart ------------------------


class TestTofuKeysPersist(unittest.TestCase):
	"""Spec §19.5 — a signing key learned at runtime via the operator-signed
	introduction path must survive a daemon restart. Without persistence the
	introduced peer is treated as first-contact again after a restart and its
	envelopes are dropped (`signature_failed`) until it re-cold-joins — a one-
	sided partition (M6). Learn a key via TOFU, save_state, reload the way
	`main.py`/`load_state` does, and assert the key still verifies without a
	fresh introduction certificate."""

	def _reload_cache(self, state, seeds_index=None):
		"""Reconstruct `daemon.signing_pubkey_cache` the way `main.py` does at
		boot: seeds ∪ persisted-membership-keys ∪ persisted-TOFU-keys."""
		cache = dict(seeds_index or {})
		for hid, rec in state.membership.items():
			if rec.signing_public_key and hid not in cache:
				cache[hid] = rec.signing_public_key
		for hid, pub in state.signing_pubkeys.items():
			if pub and hid not in cache:
				cache[hid] = pub
		return cache

	def test_introduced_key_verifies_after_reload(self):
		import tempfile

		from atlas.networkd.state import load_state, save_state

		op_priv_raw, op_pub_raw = generate_keypair_raw()
		op_priv_b64 = base64.b64encode(op_priv_raw).decode()
		op_pub_b64 = base64.b64encode(op_pub_raw).decode()
		new_priv_raw, new_pub_raw = generate_keypair_raw()
		new_priv_b64 = base64.b64encode(new_priv_raw).decode()
		new_pub_b64 = base64.b64encode(new_pub_raw).decode()

		# A live daemon-like object whose TOFU writes land on a real AppliedState.
		d = _EnvelopeDaemon(operator_public_key=op_pub_b64)
		d.state = AppliedState()  # replace the stub with a real, persistable state

		# First contact with a valid operator-signed introduction → TOFU learn.
		payload = {"host_id": "Q", "signing_public_key": new_pub_b64, "generation": 1}
		intro_sig = sign_introduction(payload, op_priv_b64)
		intro = Message(
			type="membership_advert",
			sender="Q",
			signing_public_key=new_pub_b64,
			payload=payload,
			introduction_signature=intro_sig,
		)
		intro = from_bytes(intro.to_bytes(new_priv_b64))
		default_envelope_verifier(intro, d)
		self.assertEqual(d.signing_pubkey_cache.get("Q"), new_pub_b64)
		# The TOFU key is now on the DURABLE state, not just the runtime cache.
		self.assertEqual(d.state.signing_pubkeys.get("Q"), new_pub_b64)

		with tempfile.TemporaryDirectory() as data_dir:
			save_state(d.state, data_dir)
			reloaded = load_state(data_dir)
			# Reconstruct the cache the way main.py does at boot (no seeds, no
			# membership record for Q — only the persisted TOFU key).
			d2 = _EnvelopeDaemon(operator_public_key=op_pub_b64)
			d2.state = reloaded
			d2.signing_pubkey_cache = self._reload_cache(reloaded)
			self.assertEqual(d2.signing_pubkey_cache.get("Q"), new_pub_b64)
			# A follow-up envelope from Q (NO introduction cert) verifies against
			# the reloaded cache — no re-introduction needed, no partition.
			follow_up = Message(type="gossip", sender="Q", signing_public_key=new_pub_b64, payload=[])
			follow_up = from_bytes(follow_up.to_bytes(new_priv_b64))
			default_envelope_verifier(follow_up, d2)  # must not raise


# --- seed loader: signing_public_key + operator pubkey (§19.4) --------------


class TestSeedLoadsWithSigningKeys(unittest.TestCase):
	def test_seed_entry_with_signing_public_key_is_loaded_onto_record(self):
		"""`seed.load_seed` populates the ed25519 `signing_public_key` field
		on each seeded MembershipRecord — spec §19.4 (the seed anchors the
		§19.1/§19.3 trust directory)."""
		import json
		import tempfile

		from atlas.networkd.seed import load_seed, signing_pubkey_index

		pub_a = base64.b64encode(generate_keypair_raw()[1]).decode()
		pub_b = base64.b64encode(generate_keypair_raw()[1]).decode()
		entries = [
			{
				"host_id": "A",
				"endpoint": "2001:db9::a",
				"wg_public_key": "wKA",
				"signing_public_key": pub_a,
				"mesh_address": "fdaa:0:0:a::1",
				"generation": 1,
			},
			{
				"host_id": "B",
				"endpoint": "2001:db9::b",
				"wg_public_key": "wKB",
				"signing_public_key": pub_b,
				"mesh_address": "fdaa:0:0:b::1",
				"generation": 1,
			},
		]
		with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
			fh.write(json.dumps(entries))
			fh.flush()
			path = fh.name
		try:
			records = load_seed(path)
			self.assertEqual(records[0].signing_public_key, pub_a)
			self.assertEqual(records[1].signing_public_key, pub_b)
			# The index helper produces the §19.4 trust directory seed.
			idx = signing_pubkey_index(records)
			self.assertEqual(idx, {"A": pub_a, "B": pub_b})
		finally:
			from pathlib import Path

			Path(path).unlink()

	def test_seed_entry_without_signing_public_key_defaults_to_empty(self):
		"""A seed entry that omits `signing_public_key` (a host bootstrapped
		before Stage 5+) loads as "" — the envelope verifier will demand a
		§19.5 introduction cert on first contact."""
		import json
		import tempfile

		from atlas.networkd.seed import load_seed, signing_pubkey_index

		entries = [
			{
				"host_id": "A",
				"endpoint": "2001:db9::a",
				"wg_public_key": "wKA",
				"mesh_address": "fdaa:0:0:a::1",
				"generation": 1,
			},
		]
		with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
			fh.write(json.dumps(entries))
			fh.flush()
			path = fh.name
		try:
			records = load_seed(path)
			self.assertEqual(records[0].signing_public_key, "")
			self.assertEqual(signing_pubkey_index(records), {})
		finally:
			from pathlib import Path

			Path(path).unlink()

	def test_load_operator_pubkey_returns_empty_when_file_absent(self):
		"""The operator pubkey file is opt-in (only present on a cluster
		bootstrapped with Stage 5+ signing). An absent file => "" — the
		envelope verifier fails-closed on any first-contact introduction."""
		from atlas.networkd.seed import load_operator_pubkey

		self.assertEqual(load_operator_pubkey("/nonexistent/operator-public-key"), "")

	def test_load_operator_pubkey_returns_stripped_body_when_present(self):
		"""A one-line base64 file is returned stripped."""
		import tempfile
		from pathlib import Path

		from atlas.networkd.seed import load_operator_pubkey

		pub_b64 = base64.b64encode(generate_keypair_raw()[1]).decode()
		with tempfile.NamedTemporaryFile("w", suffix="-op", delete=False) as fh:
			fh.write(pub_b64 + "\n")
			fh.flush()
			path = fh.name
		try:
			self.assertEqual(load_operator_pubkey(path), pub_b64)
		finally:
			Path(path).unlink()


if __name__ == "__main__":
	unittest.main()
