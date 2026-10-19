from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase
from frappe.utils import now_datetime
from rq.timeouts import JobTimeoutException

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.vm.core.metal_client import MetalClientError
from atlas.vm.core.metal_models import MetalHostCompatibility
from atlas.vm.core.vm_migration import MigrationService, run_migration


def migration_doc(**overrides: object) -> SimpleNamespace:
	"""Build the migration fields used by unit tests."""
	values: dict[str, object] = {
		"name": "mig-00001",
		"virtual_machine": "vm-00001",
		"source_metal_server": "metal-1",
		"destination_metal_server": "metal-2",
		"status": "preparing",
		"started_at": None,
		"progress_percent": 0,
		"destination_metal_server_selection_attempts": 0,
		"target_cpu_millicores": 0,
		"target_memory_mib": 0,
		"target_disk_mib": 0,
		"has_memory_snapshot": 0,
		"allow_disk_only_fallback": 1,
		"metal_server_drain": None,
		"error_code": None,
		"transfers": [],
	}
	values.update(overrides)
	return SimpleNamespace(**values)


def source_vm(**overrides: object) -> SimpleNamespace:
	"""Build a VM source shape used by validation tests."""
	values: dict[str, object] = {
		"name": "vm-00001",
		"active_migration": None,
		"is_draft": 0,
		"is_terminating": 0,
		"current_state": "running",
		"desired_state": "running",
		"architecture": "amd64",
		"tenant_id": 7,
		"is_network_gateway": 0,
		"cpu_millicores": 1000,
		"memory_mib": 512,
		"disk_mib": 1024,
		"sleep_after_idle_seconds": 0,
		"placement_rules": None,
	}
	values.update(overrides)
	return SimpleNamespace(**values)


class TestMigrationValidation(UnitTestCase):
	def test_rejects_an_already_migrating_vm(self) -> None:
		with self.assertRaisesRegex(AtlasUserError, "already migrating"):
			MigrationService.validate_source(source_vm(active_migration="mig-00001"))

	def test_rejects_a_vm_without_a_live_state(self) -> None:
		for state in ("failed", "unknown"):
			with self.assertRaises(AtlasUserError):
				MigrationService.create(source_vm(current_state=state))

	def test_reads_metal_state_before_locking_the_vm(self) -> None:
		with patch("frappe.get_doc", side_effect=RuntimeError("Lock requested")) as lock:

			class Source(SimpleNamespace):
				@property
				def current_state(self) -> str:
					if lock.called:
						raise AssertionError("Metal state read after the row lock")
					return "running"

			with self.assertRaisesRegex(RuntimeError, "Lock requested"):
				MigrationService.create(Source(name="vm-00001"))
			lock.assert_called_once_with("Virtual Machine", "vm-00001", for_update=True)


class TestMigrationStatus(UnitTestCase):
	def test_maps_metal_phases_to_lifecycle_statuses(self) -> None:
		service = MigrationService(migration_doc())
		self.assertEqual(service.status_from_response({"status": "running", "phase": "copying"}), "copying")
		self.assertEqual(
			service.status_from_response({"status": "running", "phase": "stopping"}), "cutting_over"
		)
		self.assertEqual(service.status_from_response({"status": "ready"}), "finalizing")

	def test_calculates_copying_progress_from_rounds(self) -> None:
		service = MigrationService(migration_doc())
		self.assertEqual(service.progress_percent("preparing", []), 5)
		self.assertEqual(
			service.progress_percent(
				"copying",
				[
					{"completed": True},
					{"completed": False, "transferred_mib": 50, "total_mib": 100},
				],
			),
			15,
		)
		self.assertEqual(service.progress_percent("starting", []), 90)

	def test_a_metal_fallback_reports_its_reason_and_keeps_the_failed_status(self) -> None:
		service = MigrationService(migration_doc(has_memory_snapshot=1, memory_snapshot_status="failed"))
		with (
			patch.object(service, "update") as update,
			patch.object(service, "release_virtual_machine"),
			patch("atlas.vm.core.vm_migration.frappe.db.commit"),
			patch("atlas.vm.core.vm_migration.count"),
			patch("atlas.vm.core.vm_migration.observe"),
			patch("atlas.vm.core.vm_migration.now_datetime", return_value="2026-09-21 12:00:00"),
		):
			service.store_progress(
				{
					"status": "ready",
					"memory_snapshot_status": "failed",
					"memory_snapshot_error": "load failed",
				}
			)
			self.assertIn("load failed", update.call_args.args[0]["error_message"])
			service.settle("completed")
		self.assertNotIn("memory_snapshot_status", update.call_args.args[0])

	def test_a_metal_fallback_keeps_a_later_failure_message(self) -> None:
		service = MigrationService(
			migration_doc(has_memory_snapshot=1, error_code="migration_error", error_message="Restore failed")
		)
		with patch.object(service, "update") as update:
			service.store_progress(
				{"status": "running", "phase": "rollback", "memory_snapshot_error": "load failed"}
			)
		self.assertNotIn("error_message", update.call_args.args[0])

	def test_aborted_destination_becomes_failed_after_a_metal_error(self) -> None:
		service = MigrationService(migration_doc(error_code="migration_error"))
		service.settle = Mock()
		self.assertTrue(service.advance({"status": "aborted"}))
		service.settle.assert_called_once_with("failed")


AFFINITY_RULES = '[{"resource": "virtual_machine", "operator": "has_not", "tags": {"role": "db"}}]'
HOST_COMPATIBILITY = MetalHostCompatibility(
	"amd64", "builtin:baseline", "GenuineIntel", "6/58", "fpu sse", "snapshot-build", 2_500_000, False
)


class TestMigrationCompatibility(UnitTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.enterContext(patch("atlas.vm.core.vm_migration._", side_effect=lambda value: value))

	def test_synced_host_comparison(self) -> None:
		cases = [
			({}, {}, True),
			({}, {"architecture": "arm64"}, False),
			({}, {"cpu_profile": "native"}, False),
			({}, {"cpu_vendor": "AuthenticAMD"}, False),
			({"cpu_profile": "native"}, {"cpu_profile": "native", "cpu_model": "6/60"}, False),
			({}, {"snapshot_host": "other-build"}, False),
			(
				{
					"cpu_profile": "custom:baseline",
					"cpu_model": "6/60",
					"cpu_features": "fpu sse avx2 fma bmi1 bmi2",
				},
				{"cpu_profile": "custom:baseline"},
				True,
			),
			({"cpu_profile": "native"}, {"cpu_profile": "native", "cpu_features": "sse"}, False),
			({"cpu_profile": "native"}, {"cpu_profile": "native"}, True),
			({}, {"tsc_khz": 0}, False),
			({"tsc_khz": 0}, {}, False),
			({}, {"tsc_khz": 2_500_625}, True),
			({}, {"tsc_khz": 2_500_626}, False),
			({}, {"tsc_khz": 3_000_000, "tsc_scaling": True}, True),
			({"architecture": "arm64", "tsc_khz": 0}, {"architecture": "arm64", "tsc_khz": 0}, True),
		]
		for source_changes, target_changes, compatible in cases:
			with self.subTest(source=source_changes, target=target_changes):
				source = replace(HOST_COMPATIBILITY, **source_changes)
				target = replace(HOST_COMPATIBILITY, **target_changes)
				service = MigrationService(migration_doc(has_memory_snapshot=True))
				with patch("atlas.vm.core.vm_migration.frappe.db.get_value") as read_report:
					read_report.side_effect = [frappe.as_json(asdict(source)), frappe.as_json(asdict(target))]
					if compatible:
						service.validate_compatibility("metal-2", source.architecture)
					else:
						with self.assertRaises(AtlasUserError):
							service.validate_compatibility("metal-2", source.architecture)

	def test_missing_facts_fall_back_to_a_disk_only_migration(self) -> None:
		service = MigrationService(migration_doc(status="scheduled", has_memory_snapshot=True))
		with (
			patch("atlas.vm.core.vm_migration.frappe.get_doc", return_value=source_vm()),
			patch("atlas.vm.core.vm_migration.now_datetime", return_value="2026-09-21 12:00:00"),
			patch("atlas.vm.core.vm_migration.PlacementStrategy.reserve_server", return_value="metal-2"),
			patch("atlas.vm.core.vm_migration.frappe.db.get_value", return_value=None),
			patch.object(service, "update") as update,
		):
			service.select_destination_metal_server()
		values = update.call_args.args[0]
		self.assertEqual(values["has_memory_snapshot"], 0)
		self.assertIn("no CPU compatibility report", values["error_message"])

	def test_a_stopped_vm_migrates_its_disk_only(self) -> None:
		service = MigrationService(migration_doc(status="scheduled", has_memory_snapshot=True))
		with (
			patch(
				"atlas.vm.core.vm_migration.frappe.get_doc",
				return_value=source_vm(current_state="stopped", desired_state="stopped"),
			),
			patch("atlas.vm.core.vm_migration.now_datetime", return_value="2026-09-21 12:00:00"),
			patch("atlas.vm.core.vm_migration.PlacementStrategy.reserve_server", return_value="metal-2"),
			patch.object(service, "update") as update,
		):
			service.select_destination_metal_server()
		self.assertEqual(update.call_args.args[0]["has_memory_snapshot"], 0)

	def test_missing_facts_fail_a_migration_without_fallback(self) -> None:
		service = MigrationService(
			migration_doc(status="scheduled", has_memory_snapshot=True, allow_disk_only_fallback=0)
		)
		with (
			patch("atlas.vm.core.vm_migration.frappe.get_doc", return_value=source_vm()),
			patch("atlas.vm.core.vm_migration.now_datetime", return_value="2026-09-21 12:00:00"),
			patch("atlas.vm.core.vm_migration.PlacementStrategy.reserve_server", return_value="metal-2"),
			patch("atlas.vm.core.vm_migration.frappe.db.get_value", return_value=None),
			patch.object(service, "update") as update,
			patch.object(service, "settle") as settle,
		):
			self.assertFalse(service.select_destination_metal_server())
		self.assertEqual(update.call_args.args[0]["error_code"], "migration_incompatible")
		settle.assert_called_once_with("failed")

	def test_memory_resize_is_rejected_before_reading_facts(self) -> None:
		service = MigrationService(migration_doc(has_memory_snapshot=True, target_memory_mib=1024))
		with patch("atlas.vm.core.vm_migration.frappe.db.get_value") as read_report:
			with self.assertRaisesRegex(AtlasUserError, "cannot resize"):
				service.validate_compatibility("metal-2", "amd64")
			read_report.assert_not_called()

	def test_disk_only_migration_does_not_read_compatibility_reports(self) -> None:
		for target_memory_mib in (0, 1024):
			with self.subTest(target_memory_mib=target_memory_mib):
				service = MigrationService(migration_doc(target_memory_mib=target_memory_mib))
				with patch("atlas.vm.core.vm_migration.frappe.db.get_value") as read_report:
					service.validate_compatibility("metal-2", "amd64")
					read_report.assert_not_called()


class TestDestinationMetalServerSelection(UnitTestCase):
	def test_a_chosen_destination_follows_the_placement_rules(self) -> None:
		service = MigrationService(migration_doc(status="scheduled", destination_metal_server=None))
		service.update = Mock()

		with (
			patch(
				"atlas.vm.core.vm_migration.frappe.get_doc",
				return_value=source_vm(placement_rules=AFFINITY_RULES),
			),
			patch("atlas.vm.core.vm_migration.now_datetime", return_value="2026-09-21 12:00:00"),
			patch(
				"atlas.vm.core.vm_migration.PlacementStrategy.find_server", return_value="metal-2"
			) as find_server,
		):
			self.assertTrue(service.select_destination_metal_server())

		requirements = find_server.call_args.args[0]
		self.assertEqual(requirements.placement_rules.as_list()[0]["tags"], {"role": "db"})
		self.assertEqual(requirements.virtual_machine, "vm-00001")

	def test_a_named_destination_ignores_the_placement_rules(self) -> None:
		service = MigrationService(migration_doc(status="scheduled", destination_metal_server="metal-3"))
		service.update = Mock()

		with (
			patch(
				"atlas.vm.core.vm_migration.frappe.get_doc",
				return_value=source_vm(placement_rules=AFFINITY_RULES),
			),
			patch("atlas.vm.core.vm_migration.now_datetime", return_value="2026-09-21 12:00:00"),
			patch(
				"atlas.vm.core.vm_migration.PlacementStrategy.reserve_server", return_value="metal-3"
			) as reserve_server,
		):
			self.assertTrue(service.select_destination_metal_server())

		self.assertEqual(reserve_server.call_args.args[0].placement_rules.nodes, ())

	def test_automatic_selection_retries_after_a_capacity_error(self) -> None:
		service = MigrationService(migration_doc(destination_metal_server=None))
		service.record_destination_metal_server_selection_failure = Mock()
		with (
			patch("atlas.vm.core.vm_migration.frappe.get_doc", return_value=source_vm()),
			patch(
				"atlas.vm.core.vm_migration.PlacementStrategy.find_server",
				side_effect=AtlasUserError("out of capacity"),
			),
		):
			self.assertFalse(service.select_destination_metal_server())
		service.record_destination_metal_server_selection_failure.assert_called_once()

	def test_reserves_a_destination_stored_on_the_scheduled_migration(self) -> None:
		service = MigrationService(migration_doc(status="scheduled", destination_metal_server="metal-3"))
		service.update = Mock()

		with (
			patch("atlas.vm.core.vm_migration.frappe.get_doc", return_value=source_vm()),
			patch("atlas.vm.core.vm_migration.now_datetime", return_value="2026-09-21 12:00:00"),
			patch(
				"atlas.vm.core.vm_migration.PlacementStrategy.reserve_server",
				return_value="metal-3",
			) as reserve_server,
		):
			self.assertTrue(service.select_destination_metal_server())

		reserve_server.assert_called_once()
		self.assertEqual(service.update.call_args.args[0]["destination_metal_server"], "metal-3")
		self.assertEqual(service.update.call_args.args[0]["status"], "preparing")

	def test_a_resize_migration_places_the_target_shape(self) -> None:
		service = MigrationService(
			migration_doc(
				status="scheduled",
				destination_metal_server=None,
				target_cpu_millicores=4000,
				target_memory_mib=8192,
				target_disk_mib=40960,
			)
		)
		service.update = Mock()

		with (
			patch("atlas.vm.core.vm_migration.frappe.get_doc", return_value=source_vm()),
			patch("atlas.vm.core.vm_migration.now_datetime", return_value="2026-09-21 12:00:00"),
			patch(
				"atlas.vm.core.vm_migration.PlacementStrategy.find_server", return_value="metal-2"
			) as find_server,
		):
			self.assertTrue(service.select_destination_metal_server())

		requirements = find_server.call_args.args[0]
		self.assertEqual(
			(requirements.cpu_millicores, requirements.memory_mib, requirements.disk_mib), (4000, 8192, 40960)
		)
		self.assertEqual(find_server.call_args.kwargs["exclude_servers"], {"metal-1"})

	def test_a_drain_migration_places_only_on_the_drain_destinations(self) -> None:
		service = MigrationService(
			migration_doc(
				status="scheduled",
				destination_metal_server=None,
				metal_server_drain="metal-server-drain-00001",
			)
		)
		service.update = Mock()
		drain = frappe.get_doc(
			{
				"doctype": "Metal Server Drain",
				"source_metal_server": "metal-1",
				"allow_any_destination": 0,
				"destinations": [{"metal_server": "metal-3"}, {"metal_server": "metal-4"}],
			}
		)
		documents = {"Virtual Machine": source_vm(), "Metal Server Drain": drain}

		with (
			patch(
				"atlas.vm.core.vm_migration.frappe.get_doc",
				side_effect=lambda doctype, _name: documents[doctype],
			),
			patch("atlas.vm.core.vm_migration.now_datetime", return_value="2026-09-21 12:00:00"),
			patch(
				"atlas.vm.core.vm_migration.PlacementStrategy.find_server", return_value="metal-3"
			) as find_server,
		):
			self.assertTrue(service.select_destination_metal_server())

		self.assertEqual(find_server.call_args.kwargs["exclude_servers"], {"metal-1"})
		self.assertEqual(find_server.call_args.kwargs["allowed_servers"], {"metal-3", "metal-4"})

	def test_a_resize_migration_sends_the_target_shape_to_the_destination(self) -> None:
		service = MigrationService(
			migration_doc(target_cpu_millicores=4000, target_memory_mib=8192, target_disk_mib=40960)
		)
		destination_client = Mock()

		with (
			patch("atlas.vm.core.vm_migration.frappe.get_doc", return_value=source_vm()),
			patch(
				"atlas.vm.core.vm_migration.MetalClient.get_coordination_url",
				return_value="https://10.0.0.1:9001",
			),
			patch.object(MigrationService, "destination_client", destination_client),
		):
			service.send_request()

		destination_client.put_migration.assert_called_once_with(
			"mig-00001",
			"vm-00001",
			"https://10.0.0.1:9001",
			{"cpu_millicores": 4000, "memory_mib": 8192, "disk_mib": 40960},
			has_memory_snapshot=False,
			allow_disk_only_fallback=True,
		)

	def test_a_plain_migration_sends_no_resize(self) -> None:
		service = MigrationService(migration_doc(has_memory_snapshot=1))
		destination_client = Mock()

		with (
			patch("atlas.vm.core.vm_migration.frappe.get_doc", return_value=source_vm()),
			patch(
				"atlas.vm.core.vm_migration.MetalClient.get_coordination_url",
				return_value="https://10.0.0.1:9001",
			),
			patch.object(MigrationService, "destination_client", destination_client),
		):
			service.send_request()

		self.assertIsNone(destination_client.put_migration.call_args.args[3])
		self.assertTrue(destination_client.put_migration.call_args.kwargs["has_memory_snapshot"])

	def test_the_commit_stores_the_server_and_the_resized_shape(self) -> None:
		virtual_machine = SimpleNamespace(name="vm-00001", server="metal-1", db_set=Mock())
		migration = SimpleNamespace(
			status="starting",
			destination_metal_server="metal-2",
			target_cpu_millicores=4000,
			target_memory_mib=8192,
			target_disk_mib=40960,
			db_set=Mock(),
		)
		service = MigrationService(migration_doc())

		with (
			patch(
				"atlas.vm.core.vm_migration.frappe.get_doc",
				side_effect=lambda doctype, *_args, **_kwargs: (
					virtual_machine if doctype == "Virtual Machine" else migration
				),
			),
			patch("atlas.vm.core.vm_migration.frappe.db.commit"),
			patch("atlas.vm.core.vm_migration.frappe.get_all", return_value=[]),
		):
			service.commit_destination()

		virtual_machine.db_set.assert_any_call("server", "metal-2")
		virtual_machine.db_set.assert_any_call(
			{"cpu_millicores": 4000, "memory_mib": 8192, "disk_mib": 40960}
		)

	def test_scheduled_abort_does_not_contact_an_unreserved_destination(self) -> None:
		service = MigrationService(migration_doc(status="scheduled", destination_metal_server="metal-2"))
		service.settle = Mock()
		service.request_destination_abort = Mock()

		with patch.object(
			service, "_lock_records", return_value=(SimpleNamespace(server="metal-1"), service.migration)
		):
			service.request_abort()

		service.settle.assert_called_once_with("aborted")
		service.request_destination_abort.assert_not_called()


class TestMigrationRunResilience(UnitTestCase):
	def test_a_recent_answer_keeps_an_old_migration_visible(self) -> None:
		service = MigrationService(migration_doc(started_at="2026-09-21 12:00:00"))
		self.assertTrue(service.is_expired)
		service.last_contact_at = now_datetime()
		self.assertFalse(service.is_expired)

	def test_a_run_survives_an_unreachable_destination_and_its_deadline(self) -> None:
		service = MigrationService(migration_doc(status="copying"))
		with (
			patch("atlas.vm.core.vm_migration.MAXIMUM_RUN_DURATION", timedelta(0)),
			patch.object(service, "send_request", side_effect=MetalClientError("offline", retryable=True)),
			patch.object(service, "record_error") as record_error,
			patch.object(service, "fail") as fail,
			patch.object(service, "request_destination_abort") as abort,
		):
			service.run()
		record_error.assert_called_once()
		fail.assert_not_called()
		abort.assert_not_called()

	def test_only_a_stuck_copy_that_nobody_canceled_fails(self) -> None:
		for status, phase, is_failed in (
			("copying", "copying", True),
			("copying", "stopping", False),
			("copying", None, False),
			("canceling", "copying", False),
		):
			response = {"status": "running", "phase": phase} if phase else {"status": "missing"}
			service = MigrationService(migration_doc(status=status, started_at="2026-09-21 12:00:00"))
			with (
				self.subTest(status=status, phase=phase),
				patch("atlas.vm.core.vm_migration.frappe.db.get_value", return_value=102400),
				patch("atlas.vm.core.vm_migration.now_datetime", return_value=datetime(2026, 9, 21, 14, 18)),
				patch("atlas.vm.core.vm_migration.MAXIMUM_RUN_DURATION", timedelta(hours=1)),
				patch.object(service, "send_request"),
				patch.object(service, "poll", return_value=response),
				patch.object(service, "advance", side_effect=[False, True]),
				patch.object(service, "fail") as fail,
				patch.object(service, "request_destination_abort") as abort,
				patch("atlas.vm.core.vm_migration.time.sleep"),
			):
				service.run()
			self.assertEqual(fail.called, is_failed)
			# A canceling run asks the destination to abort before it polls.
			self.assertEqual(abort.called, is_failed or status == "canceling")

	def test_only_a_definite_worker_error_fails_the_migration(self) -> None:
		for error, is_failed in (
			(frappe.QueryDeadlockError("deadlock"), False),
			(JobTimeoutException("job timed out"), False),
			(MetalClientError("offline", retryable=True), False),
			(ValueError("bug"), True),
		):
			with (
				self.subTest(error=type(error).__name__),
				patch("atlas.vm.core.vm_migration.frappe.get_doc", return_value=migration_doc()),
				patch("atlas.vm.core.vm_migration.frappe.db"),
				patch("atlas.vm.core.vm_migration.frappe.log_error"),
				patch.object(MigrationService, "run", side_effect=error),
				patch.object(MigrationService, "fail") as fail,
			):
				run_migration("mig-00001")
			self.assertEqual(fail.called, is_failed)


class TestMigrationLock(UnitTestCase):
	def test_run_releases_the_vm_lock_of_a_terminal_migration(self) -> None:
		service = MigrationService(migration_doc(status="aborted"))
		service.send_request = Mock()

		with patch("frappe.db.set_value") as set_value, patch("frappe.db.commit"):
			service.run()

		set_value.assert_called_once_with(
			"Virtual Machine", {"name": "vm-00001", "active_migration": "mig-00001"}, "active_migration", None
		)
		service.send_request.assert_not_called()


class TestMigrationTransfers(UnitTestCase):
	def test_upserts_each_transfer_into_its_row_index(self) -> None:
		class Document(SimpleNamespace):
			def append(self, _: str, values: dict[str, object]) -> SimpleNamespace:
				row = SimpleNamespace(**values)
				self.transfers.append(row)
				return row

		migration = Document(transfers=[])
		with patch("frappe.utils.data.get_system_timezone", return_value="UTC"):
			MigrationService.upsert_transfers(
				migration,
				[
					{
						"sequence": 1,
						"finished_at": "2026-09-20T12:00:42.02675576Z",
						"transferred_mib": 100,
						"total_mib": 100,
						"completed": True,
					},
					{
						"sequence": 2,
						"finished_at": "0001-01-01T00:00:00Z",
						"transferred_mib": 40,
						"total_mib": 100,
						"completed": False,
					},
					{"sequence": 3, "snapshot_type": "memory", "total_mib": 512, "completed": False},
				],
			)
			self.assertIsNone(migration.transfers[1].finished_at)

			MigrationService.upsert_transfers(
				migration,
				[
					{
						"sequence": 2,
						"finished_at": "2026-09-20T12:01:10Z",
						"transferred_mib": 100,
						"total_mib": 100,
						"completed": True,
					},
					{
						"sequence": 3,
						"snapshot_type": "memory",
						"transferred_mib": 512,
						"total_mib": 512,
						"completed": True,
					},
				],
			)

		self.assertEqual(len(migration.transfers), 3)
		self.assertEqual([row.idx for row in migration.transfers], [1, 2, 3])
		self.assertEqual([row.snapshot_type for row in migration.transfers], ["Disk", "Disk", "Memory"])
		self.assertEqual(migration.transfers[0].finished_at, datetime(2026, 9, 20, 12, 0, 42, 26755))
		self.assertEqual(migration.transfers[1].finished_at, datetime(2026, 9, 20, 12, 1, 10))
		self.assertEqual(migration.transfers[1].transferred_mib, 100)
		self.assertTrue(migration.transfers[1].completed)
		self.assertEqual(migration.transfers[2].transferred_mib, 512)
		self.assertTrue(migration.transfers[2].completed)
