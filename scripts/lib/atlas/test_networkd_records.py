"""Unit tests for `networkd.records` — record types, generation rules, the
effective-ownership table, and §7.3 conflict detection.

Run from `scripts/lib`: `python3 -m unittest atlas.test_networkd_records` — no
host, no Frappe, no wg. Mirrors `test_host_mesh.py`'s posture (pure functions
covered offline; host-touching pieces proven live elsewhere).
"""

import unittest

from atlas.networkd.records import (
	MembershipKind,
	MembershipRecord,
	MemberState,
	OwnershipAdvertisement,
	OwnershipTable,
	dedupe_key_membership,
	dedupe_key_ownership,
	effective_ownership,
	membership_replaces,
	ownership_replaces,
	owning_advertisement,
)


def member(host_id: str, gen: int, key: str = "k-" + __import__("secrets").token_hex(4)) -> MembershipRecord:
	return MembershipRecord(
		host_id=host_id,
		kind=MembershipKind.MEMBER,
		state=MemberState.ALIVE,
		endpoint=f"2001:db9::{host_id}",
		wg_public_key=key,
		mesh_address=f"fdaa:0:0:{host_id}::1",
		generation=gen,
	)


def adv(origin: str, gen: int, *ips: str) -> OwnershipAdvertisement:
	return owning_advertisement(origin=origin, generation=gen, owned=ips)


class TestEffectiveOwnership(unittest.TestCase):
	def test_no_records_is_empty(self):
		t = effective_ownership({})
		self.assertEqual(t.owner_of, {})
		self.assertEqual(t.conflicts, frozenset())

	def test_one_origin(self):
		t = effective_ownership({"h1": adv("h1", 1, "fdaa::1", "fdaa::2")})
		self.assertEqual(t.owner_of, {"fdaa::1": "h1", "fdaa::2": "h1"})
		self.assertEqual(t.conflicts, frozenset())

	def test_distinct_origins_distinct_ips(self):
		t = effective_ownership(
			{
				"h1": adv("h1", 1, "fdaa::1"),
				"h2": adv("h2", 1, "fdaa::2"),
			}
		)
		self.assertEqual(t.owner_of, {"fdaa::1": "h1", "fdaa::2": "h2"})
		self.assertEqual(t.conflicts, frozenset())

	def test_two_origins_same_ip_is_conflict(self):
		# Issue C close-out: never elect; the /128 goes to `conflicts`, NOT
		# `owner_of`, so §16.3 drops it from WgDesired across every host.
		t = effective_ownership(
			{
				"h1": adv("h1", 1, "fdaa::1"),
				"h2": adv("h2", 1, "fdaa::1"),  # h2 claims the same /128
			}
		)
		self.assertEqual(t.conflicts, frozenset({"fdaa::1"}))
		self.assertNotIn("fdaa::1", t.owner_of)

	def test_generations_not_compared_across_origins(self):
		# Even with wildly different generations the cross-origin claim is still
		# a conflict — a higher generation does NOT "win" (Issue C).
		t = effective_ownership(
			{
				"h1": adv("h1", 1, "fdaa::1"),
				"h2": adv("h2", 999, "fdaa::1"),
			}
		)
		self.assertEqual(t.conflicts, frozenset({"fdaa::1"}))
		self.assertNotIn("fdaa::1", t.owner_of)

	def test_conflict_and_distinct_coexist(self):
		t = effective_ownership(
			{
				"h1": adv("h1", 1, "fdaa::1", "fdaa::3"),
				"h2": adv("h2", 1, "fdaa::1", "fdaa::4"),
				"h3": adv("h3", 1, "fdaa::2"),
			}
		)
		self.assertEqual(t.owner_of, {"fdaa::2": "h3", "fdaa::3": "h1", "fdaa::4": "h2"})
		self.assertEqual(t.conflicts, frozenset({"fdaa::1"}))

	def test_three_origins_same_ip_still_one_conflict(self):
		t = effective_ownership(
			{
				"h1": adv("h1", 1, "fdaa::1"),
				"h2": adv("h2", 1, "fdaa::1"),
				"h3": adv("h3", 1, "fdaa::1"),
			}
		)
		self.assertEqual(t.conflicts, frozenset({"fdaa::1"}))
		self.assertNotIn("fdaa::1", t.owner_of)


class TestApplyRules(unittest.TestCase):
	def test_membership_replaces_higher(self):
		self.assertTrue(membership_replaces(None, member("h1", 1)))
		self.assertTrue(membership_replaces(member("h1", 1), member("h1", 2)))
		self.assertFalse(membership_replaces(member("h1", 2), member("h1", 2)))
		self.assertFalse(membership_replaces(member("h1", 5), member("h1", 1)))

	def test_ownership_replaces_higher(self):
		existing = owning_advertisement("h1", 5, ["a", "b"])
		incoming_low = owning_advertisement("h1", 4, ["a"])
		incoming_high = owning_advertisement("h1", 6, ["a"])
		self.assertTrue(ownership_replaces(None, incoming_low))
		self.assertFalse(ownership_replaces(existing, incoming_low))
		self.assertFalse(ownership_replaces(existing, existing))
		self.assertTrue(ownership_replaces(existing, incoming_high))


class TestDedupeKeys(unittest.TestCase):
	def test_membership_key_includes_origin_kind_gen(self):
		m = member("h1", 7)
		self.assertEqual(dedupe_key_membership(m), ("h1", "membership", 7))

	def test_ownership_key_includes_origin_kind_gen(self):
		a = owning_advertisement("h2", 11, ["fdaa::1"])
		self.assertEqual(dedupe_key_ownership(a), ("h2", "ownership", 11))

	def test_keys_distinguish_membership_vs_ownership_same_origin_gen(self):
		# Same origin AND same generation but different kind: distinct keys, so
		# a Membership gen-7 update and an Ownership gen-7 update don't shadow
		# each other in the §13.3 cache.
		m = member("h1", 7)
		a = owning_advertisement("h1", 7, ["fdaa::1"])
		self.assertNotEqual(dedupe_key_membership(m), dedupe_key_ownership(a))


class TestOwnershipAdvertisementEquality(unittest.TestCase):
	def test_owned_is_frozenset_order_insensitive(self):
		# §13.3 cache + §16.2 render both depend on byte-equal advertisements on
		# equal generation + set, regardless of the iteration order the caller
		# passed.
		a = owning_advertisement("h1", 1, ["fdaa::1", "fdaa::2"])
		b = owning_advertisement("h1", 1, ["fdaa::2", "fdaa::1"])
		self.assertEqual(a, b)
		self.assertEqual(hash(a), hash(b))


class TestRecordInjectionGuard(unittest.TestCase):
	"""A compromised-but-authenticated host can sign a MembershipRecord with
	`wg_public_key = "valid_key\\n[Peer]\\nPublicKey = <evil_pubkey>\\n..."` and
	the per-record signature still verifies (the attacker holds the priv mate
	of their own signing key — the §19.3 layer authenticates the AUTHOR's
	identity, not the safety of field values). Render interpolates
	`wg_public_key` / `endpoint` / `mesh_address` verbatim into wg-mesh.conf;
	`wg-quick strip` preserves all `[Peer]` sections, so a newline in any of
	those three fields injects a rogue `[Peer]` entry into every host's
	wg-mesh.conf with an attacker-controlled pubkey (whose priv mate the
	attacker actually holds — unlike the §19.2 self-forgery case where they
	don't hold the priv mate of the cluster's trusted wg key). Spec §19.2
	bounds the "compromised-host can forge its own record" damage by the peer
	slot; newline injection escapes that bound, so
	`MembershipRecord.validate()` rejects whitespace/control chars at the
	parse boundary (wire + seed), and `render.render_wg_desired` calls
	`validate()` again at the rendering doorstep as belt-and-suspenders for
	records constructed directly (bypassing parse).
	"""

	def _seed_dict(self, **overrides) -> dict:
		d = {
			"host_id": "h-ev",
			"kind": "member",
			"state": "alive",
			"endpoint": "2001:db9::1",
			"wg_public_key": "A" * 44,
			"mesh_address": "fdaa:0:0:1::1",
			"generation": 1,
		}
		d.update(overrides)
		return d

	def test_valid_record_passes_validation(self):
		# Sanity — a well-formed record is accepted by the parse path.
		from atlas.networkd.wire import membership_from_dict

		record = membership_from_dict(self._seed_dict())  # must not raise
		# Idempotent — calling validate() again also passes.
		record.validate()

	def test_wg_public_key_with_newline_rejected_at_parse(self):
		# The headline attack vector: a signed MembershipRecord whose
		# wg_public_key carries `[Peer]\nPublicKey = <evil_pubkey>` past the
		# sig check, then injects a rogue peer at render.
		from atlas.networkd.wire import membership_from_dict

		evil = "A" * 44 + "\n[Peer]\nPublicKey = " + "B" * 44 + "\nAllowedIPs = fdab::/8"
		with self.assertRaises(ValueError) as raised:
			membership_from_dict(self._seed_dict(wg_public_key=evil))
		self.assertIn("wg_public_key", str(raised.exception))

	def test_endpoint_with_newline_rejected_at_parse(self):
		from atlas.networkd.wire import membership_from_dict

		evil = "2001:db9::1\n[Peer]\nPublicKey = " + "B" * 44
		with self.assertRaises(ValueError) as raised:
			membership_from_dict(self._seed_dict(endpoint=evil))
		self.assertIn("endpoint", str(raised.exception))

	def test_mesh_address_with_newline_rejected_at_parse(self):
		from atlas.networkd.wire import membership_from_dict

		evil = "fdaa:0:0:1::1\n[Peer]\nPublicKey = " + "B" * 44
		with self.assertRaises(ValueError) as raised:
			membership_from_dict(self._seed_dict(mesh_address=evil))
		self.assertIn("mesh_address", str(raised.exception))

	def test_carriage_return_in_wg_public_key_rejected_at_parse(self):
		# `\r` is the alternate line-separator that some parsers honor; the
		# loose check (`c.isspace() or ord(c) < 32`) catches both `\r` (ord 13
		# < 32, also isspace) and `\n` (ord 10 < 32, also isspace).
		from atlas.networkd.wire import membership_from_dict

		with self.assertRaises(ValueError):
			membership_from_dict(self._seed_dict(wg_public_key="A" * 44 + "\r[Peer]"))

	def test_tab_in_wg_public_key_rejected_at_parse(self):
		from atlas.networkd.wire import membership_from_dict

		with self.assertRaises(ValueError):
			membership_from_dict(self._seed_dict(wg_public_key="A" * 44 + "\t[Peer]"))

	def test_null_byte_in_endpoint_rejected_at_parse(self):
		from atlas.networkd.wire import membership_from_dict

		with self.assertRaises(ValueError):
			membership_from_dict(self._seed_dict(endpoint="2001:db9::1\x00[Peer]"))

	def test_seed_entry_with_newline_in_wg_public_key_rejected(self):
		# The seed path is the other parse boundary; mirror coverage there.
		# Operator-controlled input should never carry newlines either, but
		# defense in depth catches a mis-fabricated seed file.
		from atlas.networkd.seed import _seed_entry_to_record

		evil = "A" * 44 + "\n[Peer]\nPublicKey = " + "B" * 44
		entry = self._seed_dict(wg_public_key=evil)
		with self.assertRaises(ValueError) as raised:
			_seed_entry_to_record(entry, "<test>")
		self.assertIn("wg_public_key", str(raised.exception))

	def test_seed_entry_with_newline_in_endpoint_rejected(self):
		from atlas.networkd.seed import _seed_entry_to_record

		entry = self._seed_dict(endpoint="2001:db9::1\n[Peer]")
		with self.assertRaises(ValueError):
			_seed_entry_to_record(entry, "<test>")

	def test_render_rejects_peer_with_whitespace_in_wg_public_key(self):
		# Belt-and-suspenders: render refuses to emit a peer whose
		# interpolated fields carry whitespace, EVEN IF the record was
		# constructed directly (bypassing the wire/seed parse validators).
		from atlas.networkd.render import render_wg_desired

		evil = MembershipRecord(
			host_id="h-ev",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::1",
			wg_public_key="A" * 44 + "\n[Peer]\nPublicKey = " + "B" * 44,
			mesh_address="fdaa:0:0:1::1",
			generation=1,
		)
		self_host = MembershipRecord(
			host_id="self",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::2",
			wg_public_key="C" * 44,
			mesh_address="fdaa:0:0:2::1",
			generation=1,
		)
		with self.assertRaises(ValueError) as raised:
			render_wg_desired("self", {"h-ev": evil, "self": self_host}, OwnershipTable())
		self.assertIn("wg_public_key", str(raised.exception))

	def test_render_rejects_peer_with_newline_in_endpoint(self):
		# Even without an injected `[Peer]` payload, a newline in `endpoint`
		# corrupts the `[{endpoint}]:{port}` wrap — wg syncconf would reject
		# the whole config (fail-closed), but render refuses to emit in the
		# first place so the conflict surfaces loud here instead of as a
		# silent wg-syncconf rejection on the host.
		from atlas.networkd.render import render_wg_desired

		evil_endpoint = MembershipRecord(
			host_id="h-ev",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::1\n[Peer]",
			wg_public_key="A" * 44,
			mesh_address="fdaa:0:0:1::1",
			generation=1,
		)
		self_host = MembershipRecord(
			host_id="self",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::2",
			wg_public_key="C" * 44,
			mesh_address="fdaa:0:0:2::1",
			generation=1,
		)
		with self.assertRaises(ValueError):
			render_wg_desired("self", {"h-ev": evil_endpoint, "self": self_host}, OwnershipTable())

	def test_render_rejects_peer_with_newline_in_mesh_address(self):
		from atlas.networkd.render import render_wg_desired

		evil_mesh = MembershipRecord(
			host_id="h-ev",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::1",
			wg_public_key="A" * 44,
			mesh_address="fdaa:0:0:1::1\n[Peer]",
			generation=1,
		)
		self_host = MembershipRecord(
			host_id="self",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::2",
			wg_public_key="C" * 44,
			mesh_address="fdaa:0:0:2::1",
			generation=1,
		)
		with self.assertRaises(ValueError):
			render_wg_desired("self", {"h-ev": evil_mesh, "self": self_host}, OwnershipTable())

	def test_render_emits_no_injected_pubkey_after_rejecting_evil_peer(self):
		# The render MUST raise BEFORE emitting any config body, so the
		# attacker's injected pubkey never reaches the bytes handed to `wg
		# syncconf`. Verify by replacing the evil peer with a valid one and
		# asserting the attacker-controlled key never appears in the body.
		from dataclasses import replace

		from atlas.networkd.render import render_wg_desired

		evil_pubkey = "B" * 44
		evil = MembershipRecord(
			host_id="h-ev",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::1",
			wg_public_key="A" * 44 + "\n[Peer]\nPublicKey = " + evil_pubkey,
			mesh_address="fdaa:0:0:1::1",
			generation=1,
		)
		self_host = MembershipRecord(
			host_id="self",
			kind=MembershipKind.MEMBER,
			state=MemberState.ALIVE,
			endpoint="2001:db9::2",
			wg_public_key="C" * 44,
			mesh_address="fdaa:0:0:2::1",
			generation=1,
		)
		# First the reject path: render raises (no body produced at all).
		with self.assertRaises(ValueError):
			render_wg_desired("self", {"h-ev": evil, "self": self_host}, OwnershipTable())
		# Now swap the evil peer for a valid one with no whitespace; render
		# succeeds and the attacker's pubkey must NOT appear in the body.
		good = replace(evil, wg_public_key="D" * 44)
		body = render_wg_desired("self", {"h-ev": good, "self": self_host}, OwnershipTable())
		self.assertNotIn(evil_pubkey, body)
		self.assertIn("D" * 44, body)


if __name__ == "__main__":
	unittest.main()
