import os
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.telemetry.collectors import WARPGATE_WAITING_SINCE_CACHE_KEY, AtlasGauges

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


class TestWarpgateSync(UnitTestCase):
	def collect(self, warpgate_url: str | None, synced_at) -> list[dict]:
		gauges = AtlasGauges("2026-10-27T12:00:00")
		with (
			patch("frappe.db.get_single_value", return_value=warpgate_url),
			patch("frappe.cache.get_value", return_value=synced_at),
		):
			gauges.add_warpgate_sync()
		return gauges.samples

	def test_the_age_counts_from_the_last_full_sync(self) -> None:
		now = AtlasGauges("2026-10-27T12:00:00").now
		samples = self.collect("https://warpgate.test", now - timedelta(minutes=20))

		self.assertAlmostEqual(samples[0]["value"], 20 * 60, delta=60)

	def test_a_region_without_warpgate_sends_nothing(self) -> None:
		self.assertEqual(self.collect(None, None), [])

	def test_a_sync_that_never_worked_counts_from_the_first_report_without_one(self) -> None:
		gauges = AtlasGauges("2026-10-27T12:00:00")
		cache = {WARPGATE_WAITING_SINCE_CACHE_KEY: gauges.now - timedelta(minutes=30)}
		with (
			patch("frappe.db.get_single_value", return_value="https://warpgate.test"),
			patch(
				"frappe.cache.get_value",
				side_effect=lambda key, generator=None: cache.get(key, generator and generator()),
			),
		):
			gauges.add_warpgate_sync()

		self.assertAlmostEqual(gauges.samples[0]["value"], 30 * 60, delta=60)


class TestMachines(UnitTestCase):
	def collect(self) -> dict[str, str]:
		rows = {
			"Metal Server": ["host-1"],
			"Virtual Machine State": [
				frappe._dict(virtual_machine="vm-service", sleep_state=""),
				frappe._dict(virtual_machine="vm-tenant", sleep_state=None),
				frappe._dict(virtual_machine="vm-sleeping", sleep_state="deep-sleeping"),
			],
			"Virtual Machine": [
				frappe._dict(name="vm-service", tenant_id=0),
				frappe._dict(name="vm-tenant", tenant_id=7),
			],
		}
		gauges = AtlasGauges("2026-10-27T12:00:00")
		with patch("frappe.get_all", side_effect=lambda doctype, **_: rows[doctype]) as get_all:
			gauges.add_machines()
		self.assertEqual(get_all.call_args.kwargs["filters"]["name"], ("in", ["vm-service", "vm-tenant"]))
		return {sample["labels"]["machine"]: sample["labels"]["kind"] for sample in gauges.samples}

	def test_each_running_machine_that_does_not_sleep_must_send(self) -> None:
		self.assertEqual(self.collect(), {"host-1": "host", "vm-service": "service", "vm-tenant": "tenant"})


def failed(method: str, minutes: int) -> SimpleNamespace:
	ended_at = datetime.now(UTC) - timedelta(minutes=minutes)
	return SimpleNamespace(kwargs={"job_name": method}, func_name="execute_job", ended_at=ended_at)


class TestJobs(UnitTestCase):
	def collect(self, jobs: list) -> list[dict]:
		registry = SimpleNamespace(count=len(jobs), get_job_ids=lambda *args, **kwargs: [])
		queue = SimpleNamespace(
			count=0,
			failed_job_registry=registry,
			started_job_registry=SimpleNamespace(count=0),
			connection=None,
			serializer=None,
		)
		gauges = AtlasGauges("2026-10-27T12:00:00")
		with (
			patch("atlas.atlas.core.telemetry.collectors.get_queue_list", return_value=["long"]),
			patch("atlas.atlas.core.telemetry.collectors.get_queue", return_value=queue),
			patch("atlas.atlas.core.telemetry.collectors.Job.fetch_many", return_value=jobs),
		):
			gauges.add_jobs()
		return [sample for sample in gauges.samples if sample["metric"] == "atlas_jobs_failed_last_hour"]

	def test_methods_that_failed_in_the_last_hour_are_named(self) -> None:
		samples = self.collect(
			[failed("atlas.vm.sync", 5), failed("atlas.vm.sync", 10), failed("atlas.host.poll", 90), None]
		)

		self.assertEqual(
			[(sample["labels"]["method"], sample["value"]) for sample in samples], [("atlas.vm.sync", 2)]
		)


class TestServices(UnitTestCase):
	def test_each_failed_node_is_named(self) -> None:
		nodes = [
			frappe._dict(name="proxy-1", status="Active", virtual_machine="vm-1"),
			frappe._dict(name="proxy-2", status="Failed", virtual_machine="vm-2"),
		]
		gauges = AtlasGauges("2026-10-27T12:00:00")
		with (
			patch(
				"frappe.get_all",
				side_effect=lambda doctype, **kwargs: nodes if doctype == "Proxy Server" else [],
			),
			patch("frappe.db.get_single_value", return_value="Active"),
		):
			gauges.add_services()

		failed = [
			sample["labels"] for sample in gauges.samples if sample["metric"] == "atlas_service_node_failed"
		]
		self.assertEqual(failed, [{"service": "proxy", "node": "proxy-2", "virtual_machine": "vm-2"}])
