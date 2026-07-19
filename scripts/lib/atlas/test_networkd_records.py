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


if __name__ == "__main__":
	unittest.main()
