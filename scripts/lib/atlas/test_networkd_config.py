"""Unit tests for `networkd.config` — TOML loading + default overlay + the
fail-loud-on-unknown-field rule.
"""

import tempfile
import unittest
from pathlib import Path

from atlas.networkd import config as cfg


class TestDefaults(unittest.TestCase):
	def test_defaults_match_spec_section_14_3(self):
		c = cfg.Config()
		self.assertEqual(c.probe_interval, 1.0)
		self.assertEqual(c.probe_timeout, 0.5)
		self.assertEqual(c.indirect_timeout, 2.0)
		self.assertEqual(c.suspect_timeout, 10.0)
		self.assertEqual(c.dead_grace, 30.0)
		self.assertEqual(c.ownership_grace, 60.0)  # > suspect + dead_grace
		self.assertEqual(c.gossip_interval, 0.2)
		self.assertEqual(c.gossip_fanout, 3)
		self.assertEqual(c.anti_entropy_interval, 1.0)
		self.assertEqual(c.seen_cache_size, 10_000)
		self.assertEqual(c.apply_debounce, 0.2)
		self.assertEqual(c.ownership_scan_interval, 2.0)
		self.assertEqual(c.advertisement_refresh_interval, 60.0)
		self.assertEqual(c.wg_host_port, 51820)
		self.assertEqual(c.wireguard_mtu, 1420)
		self.assertEqual(c.wg_device, "wg-mesh")

	def test_ownership_grace_exceeds_suspicion_window(self):
		# spec §14.3: ownership_grace must outlast suspect + dead_grace so a
		# late-refuting host doesn't lose its routes mid-refute.
		c = cfg.Config()
		self.assertGreater(c.ownership_grace, c.suspect_timeout + c.dead_grace)


class TestLoad(unittest.TestCase):
	def test_missing_file_returns_defaults(self):
		with tempfile.TemporaryDirectory() as d:
			c = cfg.load(Path(d) / "nope.toml")
			self.assertEqual(c, cfg.Config())

	def test_flat_overlays(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "ancp.toml"
			p.write_text("suspect_timeout = 60.0\ngossip_fanout = 5\n")
			c = cfg.load(p)
			self.assertEqual(c.suspect_timeout, 60.0)
			self.assertEqual(c.gossip_fanout, 5)
			# Untouched fields keep defaults.
			self.assertEqual(c.probe_interval, 1.0)

	def test_wrapped_in_ancp_table(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "ancp.toml"
			p.write_text("[ancp]\nsuspect_timeout = 90.0\n")
			c = cfg.load(p)
			self.assertEqual(c.suspect_timeout, 90.0)

	def test_unknown_table_rejected(self):
		# Fail loud at the boundary (Taste.md) — a typo'd table surface should
		# raise, not silently no-op.
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "ancp.toml"
			p.write_text("[ancp]\nsuspect_timeout = 1.0\n[typo]\nx = 1\n")
			with self.assertRaises(ValueError):
				cfg.load(p)

	def test_unknown_field_rejected(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "ancp.toml"
			p.write_text("nonsense_field = 1\n")
			with self.assertRaises(TypeError):
				cfg.load(p)


class TestWithOverrides(unittest.TestCase):
	def test_with_overrides(self):
		c = cfg.Config().with_overrides(suspect_timeout=120.0)
		self.assertEqual(c.suspect_timeout, 120.0)

	def test_with_overrides_unknown_raises(self):
		with self.assertRaises(TypeError):
			cfg.Config().with_overrides(nonexistent=1)


if __name__ == "__main__":
	unittest.main()
