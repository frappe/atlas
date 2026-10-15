# Copyright (c) 2026, Frappe and Contributors
# See license.txt

from datetime import timedelta
from unittest.mock import Mock, patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.vm.core.metal_client import MetalClientError
from atlas.vm.core.vm_migration import MigrationService

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class IntegrationTestVirtualMachineMigration(IntegrationTestCase):
	def create_migration(self) -> MigrationService:
		"""Store a migration and its VM for controller recovery checks."""
		servers = []
		for _ in range(2):
			server = frappe.get_doc({"doctype": "Metal Server", "name": str(uuid4())})
			server.db_insert()
			servers.append(server.name)
		virtual_machine = frappe.get_doc(
			{"doctype": "Virtual Machine", "name": str(uuid4()), "server": servers[0]}
		)
		virtual_machine.db_insert()
		migration = frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"name": str(uuid4()),
				"virtual_machine": virtual_machine.name,
				"source_metal_server": servers[0],
				"destination_metal_server": servers[1],
				"status": "copying",
				"started_at": now_datetime() - timedelta(minutes=11),
				"has_memory_snapshot": 1,
				"memory_snapshot_status": "not_started",
			}
		)
		migration.db_insert()
		virtual_machine.db_set("active_migration", migration.name)
		return MigrationService(migration)

	def test_cancellation_wins_over_stale_progress_and_commit(self) -> None:
		service = self.create_migration()
		stale = MigrationService(frappe.get_doc(service.migration.doctype, service.migration.name))
		client = Mock()
		with patch("frappe.db.commit"), patch.object(MigrationService, "destination_client", client):
			service.request_abort()
			self.assertFalse(stale.commit_destination())
			stale.store_progress({"status": "ready", "memory_snapshot_status": "completed"})
			self.assertEqual(stale.migration.status, "canceling")
			self.assertFalse(stale.advance({"status": "ready"}))
			self.assertEqual(
				frappe.db.get_value("Virtual Machine", service.migration.virtual_machine, "server"),
				service.migration.source_metal_server,
			)
			client.finish_migration.assert_not_called()

	def test_committed_destination_only_recovers_forward(self) -> None:
		service = self.create_migration()
		stale = MigrationService(frappe.get_doc(service.migration.doctype, service.migration.name))
		client = Mock()
		with patch("frappe.db.commit"), patch.object(MigrationService, "destination_client", client):
			self.assertTrue(service.commit_destination())
			with self.assertRaisesRegex(AtlasUserError, "committed its destination"):
				stale.request_abort()
			service.fail("migration_worker_error", "Finish request timed out")
			self.assertEqual(service.migration.status, "finalizing")
			service.request_destination_abort()
			client.finish_migration.assert_called_once_with(service.migration.name)
			client.abort_migration.assert_not_called()
			self.assertEqual(
				frappe.db.get_value("Virtual Machine", service.migration.virtual_machine, "active_migration"),
				service.migration.name,
			)

	def test_failed_rollback_keeps_the_vm_lock_until_recovery(self) -> None:
		service = self.create_migration()
		client = Mock()
		with patch("frappe.db.commit"), patch.object(MigrationService, "destination_client", client):
			self.assertTrue(service.is_expired)
			self.assertFalse(service.advance({"status": "failed", "error": {"message": "Restore failed"}}))
			self.assertEqual(service.migration.status, "canceling")
			self.assertEqual(
				frappe.db.get_value("Virtual Machine", service.migration.virtual_machine, "active_migration"),
				service.migration.name,
			)
			self.assertTrue(service.advance({"status": "aborted"}))
			self.assertEqual(service.migration.status, "failed")
			self.assertFalse(
				frappe.db.get_value("Virtual Machine", service.migration.virtual_machine, "active_migration")
			)

	def test_stale_progress_cannot_reopen_a_settled_migration(self) -> None:
		service = self.create_migration()
		stale = MigrationService(frappe.get_doc(service.migration.doctype, service.migration.name))
		with patch("frappe.db.commit"):
			service.settle("aborted")
			stale.store_progress({"status": "running", "phase": "copying"})
			self.assertEqual(stale.migration.status, "aborted")
			self.assertTrue(stale.advance({"status": "ready"}))

	def test_cancellation_remains_aborted_after_a_connection_error(self) -> None:
		service = self.create_migration()
		with patch("frappe.db.commit"), patch.object(MigrationService, "destination_client", Mock()):
			service.request_abort()
			service.record_error(MetalClientError("Destination offline", retryable=True))
			self.assertTrue(service.advance({"status": "aborted"}))
		self.assertEqual(service.migration.status, "aborted")
		self.assertEqual(service.migration.memory_snapshot_status, "aborted")
		self.assertFalse(
			frappe.db.get_value("Virtual Machine", service.migration.virtual_machine, "active_migration")
		)

	def test_connection_errors_do_not_replace_the_migration_failure(self) -> None:
		service = self.create_migration()
		stale = MigrationService(frappe.get_doc(service.migration.doctype, service.migration.name))
		with patch("frappe.db.commit"):
			service.fail("migration_error", "Restore failed")
			stale.record_error(MetalClientError("Destination offline", retryable=True))
			self.assertEqual(stale.migration.error_code, "migration_error")
			self.assertEqual(stale.migration.error_message, "Restore failed")
			self.assertTrue(stale.advance({"status": "aborted"}))
		self.assertEqual(stale.migration.status, "failed")

	def test_timeouts_do_not_fail_an_existing_cancellation(self) -> None:
		service = self.create_migration()
		stale = MigrationService(frappe.get_doc(service.migration.doctype, service.migration.name))
		with patch("frappe.db.commit"), patch.object(MigrationService, "destination_client", Mock()):
			service.request_abort()
			self.assertTrue(stale.is_expired)
			self.assertFalse(stale.advance({"status": "missing"}))
			stale.fail("migration_timeout", "The worker reached its run duration")
			self.assertTrue(stale.advance({"status": "aborted"}))
		self.assertEqual(stale.migration.status, "aborted")
		self.assertEqual(stale.migration.memory_snapshot_status, "aborted")
		self.assertFalse(
			frappe.db.get_value("Virtual Machine", service.migration.virtual_machine, "active_migration")
		)

	def test_snapshot_progress_and_terminal_results_are_persisted(self) -> None:
		server = frappe.get_doc({"doctype": "Metal Server", "name": str(uuid4())})
		server.db_insert()
		virtual_machine = frappe.get_doc({"doctype": "Virtual Machine", "name": str(uuid4())})
		virtual_machine.db_insert()
		migration = frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"name": str(uuid4()),
				"virtual_machine": virtual_machine.name,
				"source_metal_server": server.name,
				"status": "copying",
				"has_memory_snapshot": 1,
				"memory_snapshot_status": "not_started",
			}
		)
		migration.db_insert()
		service = MigrationService(migration)
		with patch("frappe.db.commit"):
			for status in ("creating", "transferring", "restoring", "completed"):
				service.store_progress({"memory_snapshot_status": status})
				self.assertEqual(
					frappe.get_doc(migration.doctype, migration.name).memory_snapshot_status, status
				)
			service.store_progress(
				{
					"status": "ready",
					"transfers": [
						{"sequence": 1, "completed": True, "transferred_mib": 10, "total_mib": 10},
						{
							"sequence": 2,
							"snapshot_type": "memory",
							"completed": True,
							"transferred_mib": 512,
							"total_mib": 512,
						},
					],
				}
			)
			self.assertEqual(service.migration.memory_snapshot_status, "completed")
			for status in ("completed", "failed", "aborted"):
				migration.db_set("status", "finalizing")
				service.settle(status)
				stored = frappe.get_doc(migration.doctype, migration.name)
				self.assertEqual(stored.memory_snapshot_status, status)
				self.assertEqual(stored.status, status)
				self.assertEqual([row.snapshot_type for row in stored.transfers], ["Disk", "Memory"])
				self.assertEqual(stored.transfers[1].transferred_mib, 512)
			migration.db_set(
				{"status": "copying", "has_memory_snapshot": 0, "memory_snapshot_status": "not_started"}
			)
			service = MigrationService(frappe.get_doc(migration.doctype, migration.name))
			service.store_progress({"memory_snapshot_status": "transferring"})
			service.settle("completed")
			self.assertEqual(service.migration.memory_snapshot_status, "not_started")

	def test_migration_uses_explicit_metal_server_field_names(self) -> None:
		meta = frappe.get_meta("Virtual Machine Migration")
		fieldnames = {field.fieldname for field in meta.fields}

		self.assertTrue(
			{
				"source_metal_server",
				"destination_metal_server",
				"destination_metal_server_selection_attempts",
			}.issubset(fieldnames)
		)
		self.assertTrue(
			{"source_server", "target_server", "target_selection_attempts", "transferred_mib"}.isdisjoint(
				fieldnames
			)
		)

	def test_source_metal_server_does_not_follow_the_virtual_machine(self) -> None:
		"""The cutover moves the VM, so a fetched value would break set_only_once."""
		field = frappe.get_meta("Virtual Machine Migration").get_field("source_metal_server")

		self.assertTrue(field.set_only_once)
		self.assertFalse(field.fetch_from)

	def test_transfer_history_uses_a_child_table(self) -> None:
		migration_meta = frappe.get_meta("Virtual Machine Migration")
		transfer_field = migration_meta.get_field("transfers")
		transfer_meta = frappe.get_meta("Virtual Machine Migration Transfer")

		self.assertEqual(transfer_field.fieldtype, "Table")
		self.assertEqual(transfer_field.options, transfer_meta.name)
		self.assertTrue(transfer_meta.istable)

	def test_migration_fields_are_read_only(self) -> None:
		meta = frappe.get_meta("Virtual Machine Migration")
		value_fields = [
			field
			for field in meta.fields
			if field.fieldtype not in {"Column Break", "Section Break", "Tab Break"}
		]

		self.assertTrue(value_fields)
		self.assertTrue(all(field.read_only for field in value_fields))
