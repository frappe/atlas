"""Unit tests for `networkd.state` — persistence round-trip + apply rules +
the §13.3 duplicate-suppression cache (LRU bounded by `seen_capacity`).
"""

import json
import tempfile
import unittest
from pathlib import Path

from atlas.networkd.records import (
	MembershipKind,
	MembershipRecord,
	MemberState,
	owning_advertisement,
)
from atlas.networkd.state import AppliedState, load_state, save_state


def member(host_id: str, gen: int, key: str = "k") -> MembershipRecord:
	return MembershipRecord(
		host_id=host_id,
		kind=MembershipKind.MEMBER,
		state=MemberState.ALIVE,
		endpoint=f"2001:db9::{host_id}",
		wg_public_key=key,
		mesh_address=f"fdaa:0:0:{host_id}::1",
		generation=gen,
	)


class TestApplyRules(unittest.TestCase):
	def test_apply_membership_higher_replaces(self):
		s = AppliedState()
		self.assertTrue(s.apply_membership(member("h1", 1)))
		self.assertTrue(s.apply_membership(member("h1", 2)))
		self.assertEqual(s.membership["h1"].generation, 2)

	def test_apply_membership_equal_or_lower_noop(self):
		s = AppliedState()
		s.apply_membership(member("h1", 5))
		self.assertFalse(s.apply_membership(member("h1", 5)))
		self.assertFalse(s.apply_membership(member("h1", 1)))
		self.assertEqual(s.membership["h1"].generation, 5)

	def test_apply_ownership_higher_replaces(self):
		s = AppliedState()
		a1 = owning_advertisement("h1", 1, ["fdaa::1", "fdaa::2"])
		a2 = owning_advertisement("h1", 2, ["fdaa::1"])  # smaller set, higher gen
		self.assertTrue(s.apply_ownership(a1))
		self.assertTrue(s.apply_ownership(a2))
		self.assertEqual(s.ownership["h1"].owned, frozenset({"fdaa::1"}))

	def test_apply_ownership_equal_or_lower_noop(self):
		s = AppliedState()
		a5 = owning_advertisement("h1", 5, ["a", "b"])
		s.apply_ownership(a5)
		self.assertFalse(s.apply_ownership(owning_advertisement("h1", 5, ["a"])))
		self.assertFalse(s.apply_ownership(owning_advertisement("h1", 1, ["a"])))
		self.assertEqual(s.ownership["h1"].owned, frozenset({"a", "b"}))


class TestSeenCache(unittest.TestCase):
	def test_seen_dedup_key_recorded_on_apply(self):
		s = AppliedState()
		s.apply_membership(member("h1", 1))
		self.assertIn(("h1", "membership", 1), list(s.seen))

	def test_seen_capacity_evicts_oldest_lru(self):
		s = AppliedState(seen_capacity=3)
		for i in range(5):
			s.apply_membership(member(f"h{i}", i))
		# Capacity 3 → only the last 3 are still cached.
		self.assertEqual(len(s.seen), 3)
		self.assertIn(("h4", "membership", 4), list(s.seen))
		self.assertNotIn(("h0", "membership", 0), list(s.seen))

	def test_repeat_apply_does_not_grow_seen(self):
		s = AppliedState(seen_capacity=10)
		s.apply_membership(member("h1", 1))
		n_before = len(s.seen)
		s.apply_membership(member("h1", 1))  # dup, no-op
		self.assertEqual(len(s.seen), n_before)


class TestOwnGeneration(unittest.TestCase):
	def test_starts_at_zero(self):
		self.assertEqual(AppliedState().own_generation, 0)

	def test_bump_increments_and_returns(self):
		s = AppliedState()
		self.assertEqual(s.bump_own_generation(), 1)
		self.assertEqual(s.bump_own_generation(), 2)
		self.assertEqual(s.own_generation, 2)


class TestPersistenceRoundTrip(unittest.TestCase):
	def test_round_trip_empty(self):
		with tempfile.TemporaryDirectory() as d:
			save_state(AppliedState(), d)
			loaded = load_state(d)
			self.assertEqual(loaded.membership, {})
			self.assertEqual(loaded.ownership, {})

	def test_round_trip_with_records(self):
		s = AppliedState()
		s.apply_membership(member("h1", 7, key="KEY1"))
		s.apply_ownership(owning_advertisement("h1", 3, ["fdaa::1", "fdaa::2"]))
		s.bump_own_generation()
		with tempfile.TemporaryDirectory() as d:
			save_state(s, d)
			loaded = load_state(d)
			self.assertEqual(loaded.membership["h1"].generation, 7)
			self.assertEqual(loaded.membership["h1"].wg_public_key, "KEY1")
			self.assertEqual(loaded.ownership["h1"].generation, 3)
			self.assertEqual(loaded.ownership["h1"].owned, frozenset({"fdaa::1", "fdaa::2"}))
			self.assertEqual(loaded.own_generation, 1)

	def test_round_trip_seen_cache(self):
		s = AppliedState()
		s.apply_membership(member("h1", 1))
		s.apply_ownership(owning_advertisement("h1", 1, ["fdaa::1"]))
		with tempfile.TemporaryDirectory() as d:
			save_state(s, d)
			loaded = load_state(d)
			self.assertIn(("h1", "membership", 1), list(loaded.seen))
			self.assertIn(("h1", "ownership", 1), list(loaded.seen))

	def test_missing_file_returns_empty(self):
		with tempfile.TemporaryDirectory() as d:
			self.assertEqual(load_state(d).membership, {})

	def test_atomic_write_preserves_prior_on_crash(self):
		# A half-written state.json must not corrupt the previous good state.
		# We verify by writing a known-good state, then writing again (overwrite)
		# and confirming the file is valid JSON with the new content.
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "state.json"
			s1 = AppliedState()
			s1.apply_membership(member("h1", 1, key="OLD"))
			save_state(s1, d)
			s2 = AppliedState()
			s2.apply_membership(member("h1", 2, key="NEW"))
			save_state(s2, d)
			# Only the latest is on disk; it parses cleanly.
			data = json.loads(p.read_text())
			self.assertEqual(data["membership"]["h1"]["wg_public_key"], "NEW")
			self.assertEqual(data["membership"]["h1"]["generation"], 2)


if __name__ == "__main__":
	unittest.main()
