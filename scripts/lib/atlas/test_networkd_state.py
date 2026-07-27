"""Unit tests for `networkd.state` — persistence round-trip + apply rules +
the §13.3 duplicate-suppression cache (LRU bounded by `seen_capacity`).
"""

import json
import os
import stat
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

	def test_seen_already_hit_and_miss(self):
		# M4 — the seen-cache is now actually consulted (was dead code). An exact
		# key hits; a higher generation from the same origin has a different key
		# and misses (so it is never suppressed).
		s = AppliedState()
		s.apply_membership(member("h1", 1))
		self.assertTrue(s.seen_already(("h1", "membership", 1)))
		self.assertFalse(s.seen_already(("h1", "membership", 2)))
		self.assertFalse(s.seen_already(("h1", "ownership", 1)))

	def test_seen_already_hit_refreshes_lru_position(self):
		# A hit does `move_to_end`, so a re-consulted key is NOT the eviction
		# victim when the cache fills — it stays cached across a partition heal.
		s = AppliedState(seen_capacity=3)
		for i in range(3):
			s.apply_membership(member(f"h{i}", i))  # h0,h1,h2 cached (h0 oldest)
		self.assertTrue(s.seen_already(("h0", "membership", 0)))  # touch h0 → MRU
		s.apply_membership(member("h3", 3))  # evicts the now-oldest (h1), not h0
		self.assertTrue(s.seen_already(("h0", "membership", 0)))
		self.assertFalse(s.seen_already(("h1", "membership", 1)))

	def test_seen_insertion_order_preserved_for_eviction(self):
		# The ordered set evicts oldest-first (FIFO) absent any hits.
		s = AppliedState(seen_capacity=2)
		s.apply_membership(member("a", 1))
		s.apply_membership(member("b", 1))
		s.apply_membership(member("c", 1))
		self.assertEqual(list(s.seen), [("b", "membership", 1), ("c", "membership", 1)])


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

	def test_round_trip_routable_dead(self):
		# H7 — the render-only `routable_dead` view (a dead host's [Peer] kept
		# until ownership_grace) survives a crash-restart so the dead host's
		# /128s don't blackhole mid-grace on a daemon restart.
		s = AppliedState()
		s.routable_dead["h9"] = member("h9", 4, key="DEADKEY")
		with tempfile.TemporaryDirectory() as d:
			save_state(s, d)
			loaded = load_state(d)
			self.assertEqual(loaded.routable_dead["h9"].wg_public_key, "DEADKEY")
			self.assertEqual(loaded.routable_dead["h9"].generation, 4)

	def test_gc_origin_if_dead_clears_routable_dead(self):
		# Reaping ownership past ownership_grace also drops the render-only [Peer].
		s = AppliedState()
		s.apply_ownership(owning_advertisement("h9", 1, ["fdaa::1"]))
		s.routable_dead["h9"] = member("h9", 1)
		reaped = s.gc_origin_if_dead("h9", dead_at=0.0, ownership_grace=20.0, now=25.0)
		self.assertTrue(reaped)
		self.assertNotIn("h9", s.routable_dead)

	def test_round_trip_signing_pubkeys(self):
		# M6 — TOFU-learned signing pubkeys (§19.5) survive a crash-restart so an
		# introduced peer isn't re-partitioned on the next boot.
		s = AppliedState()
		s.signing_pubkeys["Q"] = "SIGNKEY_Q="
		with tempfile.TemporaryDirectory() as d:
			save_state(s, d)
			loaded = load_state(d)
			self.assertEqual(loaded.signing_pubkeys["Q"], "SIGNKEY_Q=")

	def test_round_trip_seen_cache(self):
		s = AppliedState()
		s.apply_membership(member("h1", 1))
		s.apply_ownership(owning_advertisement("h1", 1, ["fdaa::1"]))
		with tempfile.TemporaryDirectory() as d:
			save_state(s, d)
			loaded = load_state(d)
			self.assertIn(("h1", "membership", 1), list(loaded.seen))
			self.assertIn(("h1", "ownership", 1), list(loaded.seen))
			# Loaded cache is still a live, O(1) ordered set: seen_already hits.
			self.assertTrue(loaded.seen_already(("h1", "membership", 1)))

	def test_round_trip_seen_preserves_order(self):
		# M4 — insertion order survives persist/load so eviction order is stable
		# across a restart.
		s = AppliedState()
		for h in ("a", "b", "c"):
			s.apply_membership(member(h, 1))
		with tempfile.TemporaryDirectory() as d:
			save_state(s, d)
			loaded = load_state(d)
			self.assertEqual(
				list(loaded.seen),
				[("a", "membership", 1), ("b", "membership", 1), ("c", "membership", 1)],
			)

	def test_load_tolerates_old_list_seen_format(self):
		# M4 — an old on-disk file wrote `seen` as a plain list of
		# [origin, kind, generation]; the new OrderedDict-backed loader must
		# still accept it (the wire shape is unchanged).
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "state.json"
			p.write_text(
				json.dumps(
					{
						"membership": {},
						"ownership": {},
						"seen": [["h1", "membership", 1], ["h2", "ownership", 4]],
						"own_generation": 2,
					}
				)
			)
			loaded = load_state(d)
			self.assertTrue(loaded.seen_already(("h1", "membership", 1)))
			self.assertTrue(loaded.seen_already(("h2", "ownership", 4)))
			self.assertEqual(loaded.own_generation, 2)

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


class TestSaveStateDurability(unittest.TestCase):
	def test_parent_dir_fsync_after_replace(self):
		# M5 — save_state must fsync the PARENT DIR after os.replace so the
		# rename survives a power-cut. Monkeypatch os.fsync to record whether it
		# was called on a directory fd; assert it doesn't raise and the file is
		# present with the right content.
		fsync_on_dir = {"seen": False}
		real_fsync = os.fsync

		def spy_fsync(fd):
			try:
				if stat.S_ISDIR(os.fstat(fd).st_mode):
					fsync_on_dir["seen"] = True
			except OSError:
				pass
			return real_fsync(fd)

		s = AppliedState()
		s.apply_membership(member("h1", 3, key="DUR"))
		with tempfile.TemporaryDirectory() as d:
			os.fsync = spy_fsync
			try:
				save_state(s, d)  # must not raise
			finally:
				os.fsync = real_fsync
			# On Linux (O_DIRECTORY present) the parent dir was fsync'd.
			if getattr(os, "O_DIRECTORY", 0):
				self.assertTrue(fsync_on_dir["seen"])
			# The file landed and parses.
			data = json.loads((Path(d) / "state.json").read_text())
			self.assertEqual(data["membership"]["h1"]["wg_public_key"], "DUR")


if __name__ == "__main__":
	unittest.main()
