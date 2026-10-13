from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch
from uuid import uuid7

import frappe
import MySQLdb
from frappe.tests import IntegrationTestCase, UnitTestCase

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.vm.core.metal_client import MetalClientError
from atlas.vm.core.vm_migration import MigrationService


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
				MigrationService.validate_source(source_vm(current_state=state))


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

	def test_aborted_destination_becomes_failed_after_a_metal_error(self) -> None:
		service = MigrationService(migration_doc(error_code="migration_error"))
		service.settle = Mock()
		self.assertTrue(service.advance({"status": "aborted"}))
		service.settle.assert_called_once_with("failed")


AFFINITY_RULES = '[{"resource": "virtual_machine", "operator": "has_not", "tags": {"role": "db"}}]'


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
		)

	def test_a_plain_migration_sends_no_resize(self) -> None:
		service = MigrationService(migration_doc())
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

	def test_the_commit_stores_the_server_and_the_resized_shape(self) -> None:
		virtual_machine = SimpleNamespace(name="vm-00001", server="metal-1", db_set=Mock())
		migration = SimpleNamespace(
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

		service.request_abort()

		service.settle.assert_called_once_with("aborted")
		service.request_destination_abort.assert_not_called()


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
					}
				],
			)

		self.assertEqual(len(migration.transfers), 2)
		self.assertEqual([row.idx for row in migration.transfers], [1, 2])
		self.assertEqual(migration.transfers[0].finished_at, datetime(2026, 9, 20, 12, 0, 42, 26755))
		self.assertEqual(migration.transfers[1].finished_at, datetime(2026, 9, 20, 12, 1, 10))
		self.assertEqual(migration.transfers[1].transferred_mib, 100)
		self.assertTrue(migration.transfers[1].completed)


class CancelIntentBase(IntegrationTestCase):
	def setUp(self) -> None:
		super().setUp()
		# Unit test mocks can leave System Settings cached as a SimpleNamespace.
		if hasattr(frappe.local, "system_settings"):
			delattr(frappe.local, "system_settings")
		try:
			frappe.client_cache.delete_value(
				frappe.get_document_cache_key("System Settings", "System Settings")
			)
		except Exception:
			pass

	def make_chain(self, initial_status: str = "copying") -> dict[str, str]:
		size = frappe.new_doc("Metal Server Size")
		size.update({"name": f"test-size-{frappe.generate_hash(length=8)}", "architecture": "amd64"})
		size.db_insert()
		image = frappe.new_doc("Metal Server Image")
		image.update({"name": f"test-image-{frappe.generate_hash(length=8)}"})
		image.db_insert()
		src = frappe.new_doc("Metal Server")
		src.update(
			{
				"name": str(uuid7()),
				"server_size": size.name,
				"server_image": image.name,
				"architecture": "amd64",
			}
		)
		src.db_insert()
		dst = frappe.new_doc("Metal Server")
		dst.update(
			{
				"name": str(uuid7()),
				"server_size": size.name,
				"server_image": image.name,
				"architecture": "amd64",
			}
		)
		dst.db_insert()
		vm = frappe.new_doc("Virtual Machine")
		vm.update(
			{
				"name": f"vm-{frappe.generate_hash(length=8)}",
				"server": src.name,
				"virtual_machine_image": "img-1",
				"architecture": "amd64",
				"cpu_millicores": 1000,
				"memory_mib": 512,
				"disk_mib": 1024,
				"tenant_id": 7,
			}
		)
		vm.db_insert()
		mig = frappe.new_doc("Virtual Machine Migration")
		mig.update(
			{
				"virtual_machine": vm.name,
				"source_metal_server": src.name,
				"destination_metal_server": dst.name,
				"status": initial_status,
			}
		)
		mig.db_insert()
		frappe.db.commit()
		names = {
			"size": size.name,
			"image": image.name,
			"src": src.name,
			"dst": dst.name,
			"vm": vm.name,
			"mig": mig.name,
		}
		self.addCleanup(self._cleanup_chain, names)
		return names

	@staticmethod
	def _cleanup_chain(names: dict[str, str]) -> None:
		frappe.db.delete("Virtual Machine Migration", {"name": names["mig"]})
		frappe.db.delete("Virtual Machine", {"name": names["vm"]})
		frappe.db.delete("Metal Server", {"name": ("in", [names["src"], names["dst"]])})
		frappe.db.delete("Metal Server Size", {"name": names["size"]})
		frappe.db.delete("Metal Server Image", {"name": names["image"]})
		frappe.db.commit()

	def mock_destination(self, mock_dc: PropertyMock) -> Mock:
		client = Mock()
		mock_dc.return_value = client
		return client

	def service_for(self, names: dict[str, str]) -> MigrationService:
		return MigrationService(frappe.get_doc("Virtual Machine Migration", names["mig"]))

	@staticmethod
	def migration_status(names: dict[str, str]) -> object:
		return frappe.db.get_value("Virtual Machine Migration", names["mig"], "status")

	@staticmethod
	def vm_server(names: dict[str, str]) -> object:
		return frappe.db.get_value("Virtual Machine", names["vm"], "server")

	@contextmanager
	def migration_client(
		self, *, abort_retryable: bool = False, poll_response: dict | None = None
	) -> Iterator[Mock]:
		with patch.object(MigrationService, "destination_client", new_callable=PropertyMock) as mock_dc:
			client = self.mock_destination(mock_dc)
			if abort_retryable:
				client.abort_migration.side_effect = MetalClientError("lost", retryable=True, uncertain=True)
			if poll_response is not None:
				client.get_migration.return_value = poll_response
			yield client

	@staticmethod
	def raw_connection_kwargs() -> dict[str, object]:
		settings = frappe.db.get_connection_settings()
		kwargs: dict[str, object] = {"user": settings["user"], "db": settings.get("database")}
		if settings.get("unix_socket"):
			kwargs["unix_socket"] = settings["unix_socket"]
		else:
			kwargs["host"] = settings.get("host") or "localhost"
			if settings.get("port"):
				kwargs["port"] = int(settings["port"])
		if settings.get("password"):
			kwargs["passwd"] = settings["password"]
		return kwargs


class TestCancelIntent(CancelIntentBase):
	def test_cancel_survives_copying_poll_and_abort_retried(self) -> None:
		names = self.make_chain("copying")
		service = self.service_for(names)
		with self.migration_client(
			abort_retryable=True,
			poll_response={"status": "running", "phase": "copying", "transfers": []},
		) as client:
			service.request_abort()
			self.assertEqual(self.migration_status(names), "canceling")
			service.advance(service.poll())
			self.assertEqual(self.migration_status(names), "canceling")
			self.assertEqual(client.abort_migration.call_count, 2)

	def test_cancel_blocks_ready_commit_and_finish(self) -> None:
		names = self.make_chain("copying")
		service = self.service_for(names)
		with self.migration_client(abort_retryable=True, poll_response={"status": "ready"}) as client:
			client.finish_migration = Mock()
			service.request_abort()
			service.advance(service.poll())

			self.assertEqual(self.vm_server(names), names["src"])
			client.finish_migration.assert_not_called()
			self.assertEqual(self.migration_status(names), "canceling")


class TestCancelIntentControls(CancelIntentBase):
	def test_control_a_copying_poll_updates_progress_when_not_canceling(self) -> None:
		names = self.make_chain("copying")
		service = self.service_for(names)
		service.store_progress(
			{
				"status": "running",
				"phase": "copying",
				"transfers": [{"sequence": 1, "transferred_mib": 50, "total_mib": 100, "completed": False}],
			}
		)
		self.assertEqual(self.migration_status(names), "copying")
		self.assertGreater(
			frappe.db.get_value("Virtual Machine Migration", names["mig"], "progress_percent"),
			0,
		)
		self.assertEqual(len(frappe.get_doc("Virtual Machine Migration", names["mig"]).transfers), 1)

	def test_control_b_completed_settles_completed(self) -> None:
		names = self.make_chain("copying")
		frappe.db.set_value("Virtual Machine", names["vm"], "active_migration", names["mig"])
		frappe.db.commit()
		service = self.service_for(names)
		service.settle = MigrationService.settle.__get__(service)
		self.assertTrue(service.advance({"status": "completed"}))
		self.assertEqual(self.migration_status(names), "completed")
		self.assertEqual(
			frappe.db.get_value("Virtual Machine Migration", names["mig"], "progress_percent"),
			100,
		)
		self.assertIsNone(frappe.db.get_value("Virtual Machine", names["vm"], "active_migration"))

	def test_control_c_aborted_settles_aborted_or_failed(self) -> None:
		for error_code, expected in ((None, "aborted"), ("migration_error", "failed")):
			with self.subTest(error_code=error_code):
				names = self.make_chain("copying")
				if error_code:
					frappe.db.set_value("Virtual Machine Migration", names["mig"], "error_code", error_code)
					frappe.db.commit()
				service = self.service_for(names)
				self.assertTrue(service.advance({"status": "aborted"}))
				self.assertEqual(self.migration_status(names), expected)

	def test_control_d_failed_records_error_and_respects_expiry(self) -> None:
		names = self.make_chain("copying")
		service = self.service_for(names)
		with self.migration_client() as client:
			client.abort_migration = Mock()

			self.assertFalse(service.advance({"status": "failed", "error": {}}))
			self.assertEqual(self.migration_status(names), "canceling")
			self.assertEqual(
				frappe.db.get_value("Virtual Machine Migration", names["mig"], "error_code"),
				"migration_error",
			)
			client.abort_migration.assert_called_once()

	def test_control_e_rollback_becomes_canceling(self) -> None:
		names = self.make_chain("copying")
		service = self.service_for(names)
		service.store_progress({"status": "running", "phase": "rollback", "transfers": []})

		self.assertEqual(self.migration_status(names), "canceling")

	def test_control_f_ready_commit_moves_vm_and_sends_finish(self) -> None:
		names = self.make_chain("copying")
		service = self.service_for(names)
		with self.migration_client() as client:
			client.finish_migration = Mock()
			client.abort_migration = Mock()
			service.advance({"status": "ready"})

			self.assertEqual(self.vm_server(names), names["dst"])
			client.finish_migration.assert_called_once_with(names["mig"])

	def test_control_g_shield_keeps_progress_transfers_duration(self) -> None:
		names = self.make_chain("canceling")
		service = self.service_for(names)
		service.store_progress(
			{
				"status": "running",
				"phase": "copying",
				"transfers": [{"sequence": 1, "transferred_mib": 50, "total_mib": 100, "completed": False}],
			}
		)

		doc = frappe.get_doc("Virtual Machine Migration", names["mig"])
		self.assertEqual(len(doc.transfers), 1)
		self.assertGreater(doc.progress_percent, 0)
		self.assertGreaterEqual(doc.duration_seconds, 0)

	def test_control_h_scheduled_abort_needs_no_destination_call(self) -> None:
		names = self.make_chain("scheduled")
		frappe.db.set_value("Virtual Machine Migration", names["mig"], "destination_metal_server", None)
		frappe.db.commit()
		service = self.service_for(names)
		service.request_destination_abort = Mock()
		service.settle = Mock()
		service.request_abort()
		service.settle.assert_called_once_with("aborted")
		service.request_destination_abort.assert_not_called()


class TestCancelIntentFreshAndWorker(CancelIntentBase):
	def test_fresh_doc_stale_service_respects_fresh_cancel(self) -> None:
		names = self.make_chain("copying")
		stale = self.service_for(names)
		fresh = self.service_for(names)
		with self.migration_client(abort_retryable=True):
			fresh.request_abort()
			self.assertEqual(stale.migration.status, "copying")
			stale.store_progress({"status": "running", "phase": "copying", "transfers": []})

			self.assertEqual(self.migration_status(names), "canceling")

	def test_already_running_worker_commit_sees_fresh_cancel(self) -> None:
		names = self.make_chain("copying")
		worker = self.service_for(names)
		with self.migration_client(poll_response={"status": "ready"}) as client:
			client.finish_migration = Mock()
			client.abort_migration = Mock()
			polled = worker.poll()

			concurrent = self.service_for(names)
			concurrent.request_abort()
			worker.advance(polled)

			self.assertEqual(self.vm_server(names), names["src"])
			client.finish_migration.assert_not_called()

	def test_cancelled_cutover_releases_row_locks(self) -> None:
		names = self.make_chain("copying")
		worker = self.service_for(names)

		def assert_locks_released() -> None:
			connection = MySQLdb.connect(**self.raw_connection_kwargs())
			try:
				cursor = connection.cursor()
				for doctype, name in (
					("Virtual Machine", names["vm"]),
					("Virtual Machine Migration", names["mig"]),
				):
					try:
						cursor.execute(
							f"SELECT name FROM `tab{doctype}` WHERE name=%s FOR UPDATE NOWAIT", (name,)
						)
					except MySQLdb.OperationalError as error:
						self.fail(f"Cannot lock {doctype} after canceled cutover: {error}")
					self.assertEqual(cursor.fetchone(), (name,))
			finally:
				connection.rollback()
				connection.close()

		def poll_without_locks(migration_id: str) -> dict[str, str]:
			self.assertEqual(migration_id, names["mig"])
			assert_locks_released()
			return {"status": "running", "phase": "copying"}

		with self.migration_client(poll_response={"status": "ready"}) as client:
			polled = worker.poll()
			self.service_for(names).request_abort()
			worker.advance(polled)

			self.assertEqual(self.vm_server(names), names["src"])
			client.finish_migration.assert_not_called()
			assert_locks_released()
			client.get_migration.side_effect = poll_without_locks
			worker.poll()
			self.assertEqual(self.migration_status(names), "canceling")


class TestCancelIntentStoreProgressRace(CancelIntentBase):
	def test_race_cancel_between_decision_and_commit_wins(self) -> None:
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		self.addCleanup(lambda: frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
		names = self.make_chain("copying")
		service = self.service_for(names)

		raw_kwargs = self.raw_connection_kwargs()
		fired = threading.Event()
		cancel_threads: list[threading.Thread] = []
		errors: list[BaseException] = []
		real_get_doc = frappe.get_doc

		def cancel_from_separate_connection() -> None:
			try:
				conn = MySQLdb.connect(**raw_kwargs)  # type: ignore[arg-type]
				try:
					cur = conn.cursor()
					cur.execute("SET SESSION innodb_lock_wait_timeout = 30")
					cur.execute(
						"UPDATE `tabVirtual Machine Migration`"
						" SET `status` = 'canceling', `modified` = NOW(6)"
						" WHERE `name` = %s",
						(names["mig"],),
					)
					conn.commit()
				finally:
					conn.close()
			except BaseException as error:
				errors.append(error)

		def hooked_get_doc(*args: object, **kwargs: object) -> object:
			doctype = args[0] if args else kwargs.get("doctype")
			name = args[1] if len(args) > 1 else kwargs.get("name")
			if (
				doctype == "Virtual Machine Migration"
				and name == names["mig"]
				and not kwargs.get("for_update")
				and not fired.is_set()
			):
				# Join only after progress commits and releases the row lock.
				fired.set()
				thread = threading.Thread(target=cancel_from_separate_connection, daemon=True)
				cancel_threads.append(thread)
				thread.start()
				time.sleep(3)
			return real_get_doc(*args, **kwargs)

		with patch("atlas.vm.core.vm_migration.frappe.get_doc", hooked_get_doc):
			service.store_progress({"status": "running", "phase": "copying", "transfers": []})
		for thread in cancel_threads:
			thread.join(timeout=30)
		self.assertFalse(any(thread.is_alive() for thread in cancel_threads))
		self.assertEqual(errors, [])

		self.assertEqual(self.migration_status(names), "canceling")

		with self.migration_client(
			abort_retryable=True,
			poll_response={"status": "running", "phase": "copying", "transfers": []},
		) as retry_client:
			fresh = self.service_for(names)
			fresh.advance(fresh.poll())
			self.assertEqual(retry_client.abort_migration.call_count, 1)
			self.assertEqual(self.migration_status(names), "canceling")
