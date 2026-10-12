# Copyright (c) 2026, Frappe and Contributors
# See license.txt

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from atlas.vm.core.vm_migration import MigrationService

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class IntegrationTestVirtualMachineMigration(IntegrationTestCase):
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
				"migrate_memory_snapshot": 1,
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
			service.store_progress({"status": "ready"})
			self.assertEqual(service.migration.memory_snapshot_status, "completed")
			for status in ("completed", "failed", "aborted"):
				service.settle(status)
				stored = frappe.get_doc(migration.doctype, migration.name)
				self.assertEqual(stored.memory_snapshot_status, status)
				self.assertEqual(stored.status, status)
			migration.db_set({"migrate_memory_snapshot": 0, "memory_snapshot_status": "not_started"})
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
