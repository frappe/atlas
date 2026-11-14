import os
from collections import namedtuple
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.telemetry.collectors import AtlasGauges

Usage = namedtuple("Usage", "total used free")
Stat = namedtuple("Stat", "st_dev")


class TestFileSystems(UnitTestCase):
	def collect(self, devices: dict[str, int]) -> list[dict]:
		gauges = AtlasGauges("2026-10-27T12:00:00")
		with (
			patch("atlas.atlas.core.telemetry.collectors.get_bench_path", return_value="/home/pilot/bench"),
			patch("frappe.db.sql", return_value=(("/var/lib/mysql/",),)),
			patch.object(os.path, "isdir", side_effect=lambda path: path in devices),
			patch.object(os, "stat", side_effect=lambda path: Stat(devices[path])),
			patch("shutil.disk_usage", return_value=Usage(100, 80, 20)),
		):
			gauges.add_file_systems()
		return gauges.samples

	def test_each_file_system_reports_its_size_and_free_space(self) -> None:
		samples = self.collect({"/": 1, "/home/pilot/bench": 2, "/var/lib/mysql/": 3})

		readings = {
			(sample["labels"]["path"], sample["labels"]["kind"]): sample["value"] for sample in samples
		}
		self.assertEqual(readings[("/", "available")], 20)
		self.assertEqual(readings[("/home/pilot/bench", "size")], 100)
		self.assertEqual(readings[("/var/lib/mysql/", "available")], 20)

	def test_a_shared_file_system_reports_once(self) -> None:
		samples = self.collect({"/": 1, "/home/pilot/bench": 1, "/var/lib/mysql/": 2})

		self.assertEqual({sample["labels"]["path"] for sample in samples}, {"/", "/var/lib/mysql/"})

	def test_a_database_on_another_server_is_skipped(self) -> None:
		samples = self.collect({"/": 1, "/home/pilot/bench": 2})

		self.assertEqual({sample["labels"]["path"] for sample in samples}, {"/", "/home/pilot/bench"})


class TestCertificates(UnitTestCase):
	def collect(self, wildcard_expires_on, servers: list[dict]) -> dict[tuple[str, str], float]:
		gauges = AtlasGauges("2026-10-27T12:00:00")
		with (
			patch("frappe.db.get_single_value", return_value=wildcard_expires_on),
			patch("frappe.get_all", return_value=[frappe._dict(server) for server in servers]),
		):
			gauges.add_certificates()
		return {
			(sample["labels"]["certificate"], sample["labels"].get("server", "")): sample["value"]
			for sample in gauges.samples
		}

	def test_each_certificate_reports_the_seconds_until_it_expires(self) -> None:
		now = AtlasGauges("2026-10-27T12:00:00").now
		readings = self.collect(
			now + timedelta(days=10),
			[{"name": "host-1", "metald_tls_expires_on": now + timedelta(days=2)}],
		)

		self.assertAlmostEqual(readings[("wildcard", "")], 10 * 86400, delta=60)
		self.assertAlmostEqual(readings[("metald", "host-1")], 2 * 86400, delta=60)

	def test_a_region_without_a_wildcard_certificate_reports_only_hosts(self) -> None:
		readings = self.collect(None, [])

		self.assertEqual(readings, {})
