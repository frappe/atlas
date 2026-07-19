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
from atlas.networkd.daemon import build_initial, default_signature_verifier
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
from atlas.networkd.signing import SignatureError, generate_keypair_raw, sign, verify
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
		state.apply_membership(MembershipRecord(
			host_id="h1",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::h1",
			wg_public_key="K",
			mesh_address="fdaa:0:0:h1::1",
			generation=5,
			signing_public_key=real_pub_b64,
		))
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


if __name__ == "__main__":
	unittest.main()
