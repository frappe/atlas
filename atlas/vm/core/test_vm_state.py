from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from uuid import uuid7

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from atlas.vm.core import vm_state


class TestVirtualMachineState(UnitTestCase):
	def store(
		self, names: list[str], stored: list[str], reported: dict, save_error: Exception | None = None
	) -> dict:
		"""Store one reported state set against a stubbed database."""
		saved = []

		def make_document(*args) -> MagicMock:
			document = MagicMock()
			document.save.side_effect = save_error
			saved.append(document)
			return document

		with (
			patch.object(vm_state, "now_datetime", return_value=datetime(2026, 9, 9, 10, 0)),
			patch.object(vm_state.frappe, "get_all", side_effect=[names, stored]),
			patch.object(vm_state.frappe, "get_doc", side_effect=make_document),
			patch.object(vm_state.frappe, "new_doc", side_effect=make_document),
			patch.object(vm_state.frappe, "log_error"),
			patch.object(vm_state.frappe.db, "commit") as commit,
		):
			vm_state.store_reported_states("metal-1", reported)

		return {"saved": saved, "commit": commit}

	def test_a_first_report_inserts_and_a_later_report_updates(self) -> None:
		calls = self.store(
			names=["vm-00001", "vm-00002"],
			stored=["vm-00002"],
			reported={"vm-00001": {"status": "running"}, "vm-00002": {"status": "running"}},
		)

		inserted, updated = calls["saved"]
		self.assertEqual(inserted.virtual_machine, "vm-00001")
		self.assertEqual(inserted.server, "metal-1")
		self.assertEqual(updated.server, "metal-1")
		self.assertEqual(updated.status, "running")
		self.assertEqual(updated.synced_at, datetime(2026, 9, 9, 10, 0))

	def test_every_reported_virtual_machine_is_saved_as_a_document(self) -> None:
		calls = self.store(
			names=["vm-00001", "vm-00002", "vm-00003"],
			stored=["vm-00001", "vm-00002", "vm-00003"],
			reported={
				"vm-00001": {"status": "running"},
				"vm-00002": {"status": "running"},
				"vm-00003": {"status": "stopped"},
			},
		)

		self.assertEqual([document.status for document in calls["saved"]], ["running", "running", "stopped"])
		self.assertEqual(calls["commit"].call_count, 3)

	def test_an_unreported_virtual_machine_keeps_its_stored_state(self) -> None:
		calls = self.store(
			names=["vm-00001", "vm-00002"],
			stored=["vm-00001", "vm-00002"],
			reported={"vm-00001": {"status": "running"}},
		)

		self.assertEqual(len(calls["saved"]), 1)

	def test_a_host_without_virtual_machines_reads_nothing_more(self) -> None:
		get_all = MagicMock(side_effect=[[]])

		with patch.object(vm_state.frappe, "get_all", get_all):
			vm_state.store_reported_states("metal-1", {})

		get_all.assert_called_once()

	def test_an_invalid_response_is_rejected(self) -> None:
		for reported in ([], {"vm-00001": "running"}, {"vm-00001": {"status": ""}}, {"vm-00001": {}}):
			with self.assertRaises(ValueError):
				vm_state.get_reported_statuses(reported)

	def test_one_failed_write_does_not_stop_the_other_writes(self) -> None:
		calls = self.store(
			names=["vm-00001", "vm-00002"],
			stored=["vm-00001", "vm-00002"],
			reported={"vm-00001": {"status": "running"}, "vm-00002": {"status": "running"}},
			save_error=ValueError("boom"),
		)

		self.assertEqual(len(calls["saved"]), 2)
		self.assertEqual(calls["commit"].call_count, 0)


class TestLiveVirtualMachineLookup(IntegrationTestCase):
	"""The image deletion guard reads the stored host state, not the virtual machine row alone."""

	def setUp(self) -> None:
		self.image_name = frappe.generate_hash(length=10)

	def insert_virtual_machine(self, **overrides) -> str:
		"""Write one virtual machine row that uses the image of this test.

		The row skips validation, because this test needs the stored shape and not
		a live host, a server, or a Metal request.
		"""
		virtual_machine = frappe.new_doc("Virtual Machine")
		virtual_machine.update(
			{
				"name": frappe.generate_hash(length=10),
				"server": str(uuid7()),
				"virtual_machine_image": self.image_name,
				"architecture": "amd64",
				"cpu_millicores": 1000,
				"memory_mib": 1024,
				"disk_mib": 10240,
				"tenant_id": 7,
				**overrides,
			}
		)
		virtual_machine.db_insert()
		return virtual_machine.name

	def store_state(self, name: str, status: str) -> None:
		"""Write one reported host state for a virtual machine."""
		state = frappe.new_doc("Virtual Machine State")
		state.update({"name": name, "virtual_machine": name, "status": status})
		state.db_insert()

	def test_a_running_virtual_machine_holds_the_image(self) -> None:
		self.store_state(self.insert_virtual_machine(), "running")

		self.assertTrue(vm_state.has_live_virtual_machine_for_image(self.image_name))

	def test_a_stopped_virtual_machine_holds_the_image(self) -> None:
		self.store_state(self.insert_virtual_machine(), "stopped")

		self.assertTrue(vm_state.has_live_virtual_machine_for_image(self.image_name))

	def test_a_draft_holds_the_image_before_any_reported_state(self) -> None:
		self.insert_virtual_machine(is_draft=1)

		self.assertTrue(vm_state.has_live_virtual_machine_for_image(self.image_name))

	def test_a_virtual_machine_with_no_reported_state_does_not_hold_the_image(self) -> None:
		self.insert_virtual_machine()

		self.assertFalse(vm_state.has_live_virtual_machine_for_image(self.image_name))

	def test_a_terminating_virtual_machine_does_not_hold_the_image(self) -> None:
		name = self.insert_virtual_machine(is_terminating=1)
		self.store_state(name, "running")

		self.assertFalse(vm_state.has_live_virtual_machine_for_image(self.image_name))

	def test_an_image_with_no_virtual_machine_is_free(self) -> None:
		self.assertFalse(vm_state.has_live_virtual_machine_for_image(self.image_name))
