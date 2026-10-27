import os
from collections import namedtuple
from unittest.mock import patch

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
