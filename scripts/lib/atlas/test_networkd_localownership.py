"""Unit tests for `networkd.localownership` + `networkd.seed`."""

import json
import tempfile
import unittest
from pathlib import Path

from atlas.networkd.localownership import read_local_ownership, same_set
from atlas.networkd.records import MembershipKind, MemberState
from atlas.networkd.seed import load_seed, load_seed_optional


class TestReadLocalOwnership(unittest.TestCase):
	def test_missing_file_returns_empty(self):
		with tempfile.TemporaryDirectory() as d:
			self.assertEqual(read_local_ownership(Path(d) / "nope.json"), frozenset())

	def test_reads_owned_list(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "lo.json"
			p.write_text(json.dumps({"owned": ["fdaa::1", "fdaa::2"]}))
			self.assertEqual(read_local_ownership(str(p)), frozenset({"fdaa::1", "fdaa::2"}))

	def test_empty_owned_is_legitimate(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "lo.json"
			p.write_text(json.dumps({"owned": []}))
			self.assertEqual(read_local_ownership(str(p)), frozenset())

	def test_malformed_not_a_dict_raises(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "lo.json"
			p.write_text(json.dumps(["fdaa::1"]))
			with self.assertRaises(ValueError):
				read_local_ownership(str(p))

	def test_missing_owned_key_raises(self):
		# Fail loud at the boundary — a corrupt cache should surface, not
		# silently re-advertise gen+1 with an empty set (which would withdraw
		# routes the host should still be carrying).
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "lo.json"
			p.write_text(json.dumps({"addrs": ["fdaa::1"]}))
			with self.assertRaises(ValueError):
				read_local_ownership(str(p))


class TestSameSet(unittest.TestCase):
	def test_equal_sets(self):
		self.assertTrue(same_set(frozenset({"a", "b"}), frozenset({"a", "b"})))

	def test_different_sets(self):
		self.assertFalse(same_set(frozenset({"a", "b"}), frozenset({"a"})))

	def test_order_insensitive(self):
		self.assertTrue(same_set(frozenset({"a", "b"}), frozenset({"b", "a"})))


class TestLoadSeed(unittest.TestCase):
	def _entry(self, host_id: str, key: str = "K") -> dict:
		return {
			"host_id": host_id,
			"endpoint": f"2001:db9::{host_id}",
			"wg_public_key": key,
			"mesh_address": f"fdaa:0:0:{host_id}::1",
			"generation": 1,
		}

	def test_load_seed_returns_membership_records(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "seed.json"
			p.write_text(json.dumps([self._entry("h1", "K1"), self._entry("h2", "K2")]))
			seeds = load_seed(str(p))
			self.assertEqual(len(seeds), 2)
			self.assertEqual(seeds[0].host_id, "h1")
			self.assertEqual(seeds[0].wg_public_key, "K1")
			self.assertEqual(seeds[0].kind, MembershipKind.MEMBER)
			self.assertEqual(seeds[0].state, MemberState.ALIVE)
			self.assertEqual(seeds[0].generation, 1)

	def test_missing_file_raises(self):
		# `load_seed` raises (a fresh host with no seed cannot join); use
		# `load_seed_optional` for the come-up-peer-empty posture.
		with tempfile.TemporaryDirectory() as d:
			with self.assertRaises(FileNotFoundError):
				load_seed(str(Path(d) / "nope.json"))

	def test_optional_returns_empty_when_absent(self):
		with tempfile.TemporaryDirectory() as d:
			self.assertEqual(load_seed_optional(str(Path(d) / "nope.json")), [])

	def test_malformed_entry_raises(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "seed.json"
			p.write_text(json.dumps([{"host_id": "h1", "endpoint": "2001:db9::h1"}]))  # missing keys
			with self.assertRaises(ValueError):
				load_seed(str(p))

	def test_malformed_top_level_raises(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "seed.json"
			p.write_text(json.dumps({"not": "a list"}))
			with self.assertRaises(ValueError):
				load_seed(str(p))


if __name__ == "__main__":
	unittest.main()
