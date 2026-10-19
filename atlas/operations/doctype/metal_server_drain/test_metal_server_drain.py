# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.operations.doctype.metal_server_drain.metal_server_drain import (
	MetalServerDrain,
	run_metal_server_drain_steps,
)

MODULE = "atlas.operations.doctype.metal_server_drain.metal_server_drain"


def insert_server() -> str:
	name = str(uuid4())
	frappe.get_doc(
		{
			"doctype": "Metal Server",
			"name": name,
			"title": name,
			"server_size": "test-size",
			"server_image": "test-image",
		}
	).db_insert()
	return name


class TestMetalServerDrain(IntegrationTestCase):
	def setUp(self) -> None:
		self.addCleanup(frappe.db.rollback)
		self.source = insert_server()
		self.destination = insert_server()

	def new_drain(self, destinations: list[str]) -> MetalServerDrain:
		return frappe.get_doc(
			{
				"doctype": "Metal Server Drain",
				"source_metal_server": self.source,
				"allow_any_destination": 0,
				"destinations": [{"metal_server": server} for server in destinations],
			}
		)

	def test_rejects_the_source_as_a_destination(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be a destination"):
			self.new_drain([self.source]).insert()

	def test_rejects_a_duplicate_destination(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "only once"):
			self.new_drain([self.destination, self.destination]).insert()

	def test_requires_at_least_one_parallel_migration(self) -> None:
		drain = self.new_drain([self.destination])
		drain.parallel_migrations = 0
		with self.assertRaisesRegex(frappe.ValidationError, "at least 1"):
			drain.insert()

	def test_requires_a_destination_unless_any_is_allowed(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "Add a destination"):
			self.new_drain([]).insert()

		drain = self.new_drain([])
		drain.allow_any_destination = 1
		drain.insert()


class TestMetalServerDrainSteps(IntegrationTestCase):
	def setUp(self) -> None:
		self.addCleanup(frappe.db.rollback)
		self.source = insert_server()
		self.destination = insert_server()

	def insert_virtual_machine(self) -> str:
		virtual_machine = frappe.get_doc(
			{"doctype": "Virtual Machine", "name": str(uuid4()), "server": self.source}
		)
		virtual_machine.db_insert()
		return virtual_machine.name

	def submit_drain(self, retry_failed_migrations: int = 0, parallel_migrations: int = 1) -> str:
		drain = frappe.get_doc(
			{
				"doctype": "Metal Server Drain",
				"source_metal_server": self.source,
				"retry_failed_migrations": retry_failed_migrations,
				"parallel_migrations": parallel_migrations,
			}
		).insert()
		drain.submit()
		return drain.name

	def create_migration(self, virtual_machine, metal_server_drain: str, **_options) -> str:
		migration = frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"name": str(uuid4()),
				"virtual_machine": virtual_machine.name,
				"source_metal_server": self.source,
				"metal_server_drain": metal_server_drain,
				"status": "scheduled",
			}
		)
		migration.db_insert()
		return migration.name

	def step(self, drain_name: str) -> MetalServerDrain:
		with (
			patch(f"{MODULE}.MigrationService.create", side_effect=self.create_migration),
			patch.object(frappe.db, "commit"),
		):
			frappe.get_doc("Metal Server Drain", drain_name).run_next_step()
		return frappe.get_doc("Metal Server Drain", drain_name)

	def settle_last_migration(self, drain: MetalServerDrain, status: str) -> None:
		self.settle_migration(drain.migrations[-1], status)

	def settle_migration(
		self, row, status: str, error_code: str | None = None, error: str | None = None
	) -> None:
		migration = frappe.get_doc("Virtual Machine Migration", row.virtual_machine_migration)
		migration.db_set({"status": status, "error_code": error_code, "error_message": error})
		if status == "completed":
			frappe.db.set_value("Virtual Machine", row.virtual_machine, "server", self.destination)

	def test_moves_one_virtual_machine_at_a_time_until_the_source_is_empty(self) -> None:
		first, second = self.insert_virtual_machine(), self.insert_virtual_machine()
		name = self.submit_drain()
		self.assertTrue(frappe.db.get_value("Metal Server", self.source, "is_in_maintenance"))

		drain = self.step(name)
		drain = self.step(name)
		self.assertEqual([row.virtual_machine for row in drain.migrations], [first])

		self.settle_last_migration(drain, "completed")
		drain = self.step(name)
		self.assertEqual([row.virtual_machine for row in drain.migrations], [first, second])
		self.assertEqual(drain.progress_percent, 50)

		self.settle_last_migration(drain, "completed")
		drain = self.step(name)
		self.assertEqual(drain.status, "Completed")
		self.assertEqual(drain.progress_percent, 100)
		self.assertEqual([row.status for row in drain.migrations], ["completed", "completed"])

	def test_fills_every_free_parallel_slot_in_one_step(self) -> None:
		first, second, third = (self.insert_virtual_machine() for _ in range(3))
		name = self.submit_drain(parallel_migrations=2)

		drain = self.step(name)
		self.assertEqual([row.virtual_machine for row in drain.migrations], [first, second])
		drain = self.step(name)
		self.assertEqual([row.virtual_machine for row in drain.migrations], [first, second])

		self.settle_last_migration(drain, "completed")
		drain = self.step(name)
		self.assertEqual([row.virtual_machine for row in drain.migrations], [first, second, third])

	def test_rows_and_the_drain_explain_why_a_virtual_machine_did_not_move(self) -> None:
		failed, moved = self.insert_virtual_machine(), self.insert_virtual_machine()
		name = self.submit_drain(parallel_migrations=2)
		drain = self.step(name)

		self.settle_migration(drain.migrations[0], "failed", "destination_unreachable", "No answer.")
		# A completed migration can keep a transient client error. Its row shows no error.
		self.settle_migration(drain.migrations[1], "completed", "metal_client_error", "Read timed out.")
		drain = self.step(name)

		self.assertEqual(
			[row.error_message for row in drain.migrations], ["destination_unreachable: No answer.", None]
		)
		self.assertEqual(drain.status, "Failed")
		self.assertEqual(
			drain.error_message,
			f"Virtual machines still on the source: 1.\n\n{failed}, 1 attempt:\n"
			"- destination_unreachable: No answer. (x1)",
		)
		self.assertNotIn(moved, drain.error_message)

	def test_completion_repairs_a_row_that_missed_its_migration_change(self) -> None:
		self.insert_virtual_machine()
		name = self.submit_drain()
		row = self.step(name).migrations[-1]

		# A direct write skips on_change, as a worker with older code would.
		frappe.db.set_value("Virtual Machine Migration", row.virtual_machine_migration, "status", "completed")
		frappe.db.set_value("Virtual Machine", row.virtual_machine, "server", self.destination)
		drain = self.step(name)

		self.assertEqual(drain.status, "Completed")
		self.assertEqual(drain.migrations[-1].status, "completed")
		self.assertEqual(drain.progress_percent, 100)

	def test_waits_while_another_migration_moves_a_virtual_machine_off_the_source(self) -> None:
		virtual_machine = self.insert_virtual_machine()
		migration = self.create_migration(frappe.get_doc("Virtual Machine", virtual_machine), None)
		frappe.db.set_value("Virtual Machine", virtual_machine, "active_migration", migration)
		name = self.submit_drain()

		drain = self.step(name)

		self.assertEqual(drain.status, "Running")
		self.assertEqual(drain.migrations, [])

	def test_retries_a_failed_virtual_machine_after_the_others(self) -> None:
		first, second = self.insert_virtual_machine(), self.insert_virtual_machine()
		name = self.submit_drain(retry_failed_migrations=1)

		drain = self.step(name)
		for status, error_code in (
			("failed", "copy_timeout"),
			("completed", None),
			("failed", "copy_timeout"),
			("failed", "transfer_failed"),
		):
			self.settle_migration(drain.migrations[-1], status, error_code)
			drain = self.step(name)

		self.assertEqual([row.virtual_machine for row in drain.migrations], [first, second, first, first])
		self.assertEqual(drain.status, "Failed")
		self.assertEqual(
			drain.error_message,
			f"Virtual machines still on the source: 1.\n\n{first}, 3 attempts:\n"
			"- copy_timeout (x2)\n- transfer_failed (x1)",
		)

	def test_a_virtual_machine_that_cannot_start_fails_the_drain_without_retries(self) -> None:
		virtual_machine = self.insert_virtual_machine()
		name = self.submit_drain()

		with (
			patch(f"{MODULE}.MigrationService.create", side_effect=AtlasUserError("not ready")),
			patch.object(frappe.db, "rollback"),
			patch.object(frappe.db, "commit"),
		):
			frappe.get_doc("Metal Server Drain", name).run_next_step()
		drain = self.step(name)

		self.assertEqual(
			[
				(row.virtual_machine, row.virtual_machine_migration, row.status, row.error_message)
				for row in drain.migrations
			],
			[(virtual_machine, None, "failed", "not ready")],
		)
		self.assertEqual(drain.status, "Failed")
		self.assertIn(f"{virtual_machine}, 1 attempt:\n- not ready (x1)", drain.error_message)

	def test_a_failed_start_is_retried_in_a_later_step(self) -> None:
		self.insert_virtual_machine()
		name = self.submit_drain(retry_failed_migrations=1, parallel_migrations=2)

		with (
			patch(f"{MODULE}.MigrationService.create", side_effect=AtlasUserError("not ready")),
			patch.object(frappe.db, "rollback"),
			patch.object(frappe.db, "commit"),
		):
			frappe.get_doc("Metal Server Drain", name).run_next_step()

		self.assertEqual(len(frappe.get_doc("Metal Server Drain", name).migrations), 1)

	def test_a_cancelled_drain_still_records_the_result_of_its_migration(self) -> None:
		self.insert_virtual_machine()
		name = self.submit_drain()
		drain = self.step(name)
		frappe.get_doc("Metal Server Drain", name).cancel()

		self.settle_last_migration(drain, "failed")

		drain = frappe.get_doc("Metal Server Drain", name)
		self.assertEqual(drain.status, "Cancelled")
		self.assertEqual(drain.migrations[-1].status, "failed")

	def test_a_cancelled_drain_starts_no_migration(self) -> None:
		self.insert_virtual_machine()
		name = self.submit_drain()
		frappe.get_doc("Metal Server Drain", name).cancel()

		with (
			patch.object(MetalServerDrain, "run_next_step") as run_next_step,
			patch.object(frappe.db, "commit"),
		):
			run_metal_server_drain_steps()

		run_next_step.assert_not_called()
		self.assertEqual(frappe.db.get_value("Metal Server Drain", name, "status"), "Cancelled")
