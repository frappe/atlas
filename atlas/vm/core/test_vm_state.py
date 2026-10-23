from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch
from uuid import uuid7

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from atlas.vm.core import vm_state


def stored_row(name: str, status: str = "stopped", server: str = "metal-1") -> frappe._dict:
	return frappe._dict(name=name, status=status, server=server)


class TestVirtualMachineState(UnitTestCase):
	def store(
		self,
		names: list[str],
		stored: list[frappe._dict],
		reported: dict,
		save_error: Exception | None = None,
	) -> dict:
		"""Store one reported state set against a stubbed database."""
		saved = []

		def make_document(*args) -> frappe._dict:
			document = frappe._dict(save=MagicMock(side_effect=save_error))
			saved.append(document)
			return document

		with (
			patch.object(vm_state, "now_datetime", return_value=datetime(2026, 9, 9, 10, 0)),
			patch.object(vm_state.frappe, "get_all", side_effect=[names, stored]),
			patch.object(vm_state.frappe, "get_doc", side_effect=make_document),
			patch.object(vm_state.frappe, "new_doc", side_effect=make_document),
			patch.object(vm_state.frappe, "log_error"),
			patch.object(vm_state, "report_unknown_virtual_machines"),
			patch.object(vm_state, "update_reported_states") as update,
			patch.object(vm_state.frappe.db, "commit") as commit,
		):
			vm_state.store_reported_states("metal-1", reported)

		return {"saved": saved, "commit": commit, "update": update}

	def test_a_first_report_inserts_and_a_changed_report_updates(self) -> None:
		calls = self.store(
			names=["vm-00001", "vm-00002"],
			stored=[stored_row("vm-00002")],
			reported={"vm-00001": {"status": "running"}, "vm-00002": {"status": "running"}},
		)

		inserted, updated = calls["saved"]
		self.assertEqual(inserted.virtual_machine, "vm-00001")
		self.assertEqual(inserted.server, "metal-1")
		self.assertEqual(updated.server, "metal-1")
		self.assertEqual(updated.status, "running")
		self.assertEqual(updated.synced_at, datetime(2026, 9, 9, 10, 0))

	def test_an_unchanged_state_only_moves_its_sync_time(self) -> None:
		calls = self.store(
			names=["vm-00001", "vm-00002", "vm-00003"],
			stored=[
				stored_row("vm-00001", "running"),
				stored_row("vm-00002", "running", server="metal-2"),
				stored_row("vm-00003", "running"),
			],
			reported={
				"vm-00001": {"status": "running"},
				"vm-00002": {"status": "running"},
				"vm-00003": {"status": "stopped"},
			},
		)

		# A moved VM and a new status are saved as documents, so their webhooks run.
		self.assertEqual([document.server for document in calls["saved"]], ["metal-1", "metal-1"])
		self.assertEqual([document.status for document in calls["saved"]], ["running", "stopped"])
		calls["update"].assert_called_once()
		self.assertEqual(list(calls["update"].call_args.args[0]), ["vm-00001"])
		self.assertEqual(calls["commit"].call_count, 2)

	def test_an_unreported_virtual_machine_keeps_its_stored_state(self) -> None:
		calls = self.store(
			names=["vm-00001", "vm-00002"],
			stored=[stored_row("vm-00001"), stored_row("vm-00002")],
			reported={"vm-00001": {"status": "running"}},
		)

		self.assertEqual(len(calls["saved"]), 1)
		calls["update"].assert_not_called()

	def test_a_host_without_virtual_machines_reads_nothing_more(self) -> None:
		get_all = MagicMock(side_effect=[[]])

		with (
			patch.object(vm_state.frappe, "get_all", get_all),
			patch.object(vm_state, "report_unknown_virtual_machines"),
		):
			vm_state.store_reported_states("metal-1", {})

		get_all.assert_called_once()

	def test_a_vm_without_an_atlas_record_is_logged_once(self) -> None:
		frappe.cache.delete_value("atlas:unknown-virtual-machines:metal-1")
		with (
			patch.object(vm_state.frappe, "get_all", return_value=["vm-00001"]),
			patch.object(vm_state.frappe, "log_error") as log_error,
		):
			vm_state.report_unknown_virtual_machines("metal-1", ["vm-00001", "vm-00009"])
			vm_state.report_unknown_virtual_machines("metal-1", ["vm-00001", "vm-00009"])

		log_error.assert_called_once()
		self.assertIn("vm-00009", log_error.call_args.kwargs["message"])
		self.assertNotIn("vm-00001", log_error.call_args.kwargs["message"])

	def test_unreadable_records_on_a_host_are_logged_once(self) -> None:
		frappe.cache.delete_value("atlas:invalid-virtual-machines:metal-1")
		with patch.object(vm_state.frappe, "log_error") as log_error:
			vm_state.report_invalid_virtual_machines("metal-1", ["vm-00002"])
			vm_state.report_invalid_virtual_machines("metal-1", ["vm-00002"])
			vm_state.report_invalid_virtual_machines("metal-1", [])

		log_error.assert_called_once()
		self.assertIn("vm-00002", log_error.call_args.kwargs["message"])
		self.assertEqual(
			log_error.call_args.kwargs["title"], "Server metal-1 cannot read Virtual Machine records"
		)

	def test_an_invalid_response_is_rejected(self) -> None:
		for reported in (
			[],
			{"vm-00001": "running"},
			{"vm-00001": {"status": ""}},
			{"vm-00001": {}},
			{"vm-00001": {"status": "stopped", "sleep_state": "dreaming"}},
		):
			with self.assertRaises(ValueError):
				vm_state.get_reported_states(reported)

	def test_one_failed_write_does_not_stop_the_other_writes(self) -> None:
		calls = self.store(
			names=["vm-00001", "vm-00002"],
			stored=[stored_row("vm-00001"), stored_row("vm-00002")],
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

	def store_state(self, name: str, status: str, **values) -> None:
		"""Write one reported host state for a virtual machine."""
		state = frappe.new_doc("Virtual Machine State")
		state.update({"name": name, "virtual_machine": name, "status": status, **values})
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

	def test_reported_values_of_many_rows_update_together(self) -> None:
		asleep, awake = self.insert_virtual_machine(), self.insert_virtual_machine()
		self.store_state(asleep, "stopped", sleep_urls_snapshot_id="checkpoint-1")
		self.store_state(awake, "running", sleep_state="sleeping", sleep_snapshot_id="checkpoint-0")
		synced_at = datetime(2026, 10, 23, 12, 0)
		requested_at = datetime(2026, 10, 23, 11, 0)

		vm_state.update_reported_states(
			{
				asleep: frappe._dict(
					sleep_state="deep-sleeping",
					sleep_snapshot_id="checkpoint-1",
					last_request_at=requested_at,
				),
				awake: frappe._dict(sleep_state="", sleep_snapshot_id=None, last_request_at=None),
			},
			synced_at,
		)

		fields = [
			"sleep_state",
			"sleep_snapshot_id",
			"sleep_urls_snapshot_id",
			"last_request_at",
			"synced_at",
		]
		self.assertEqual(
			frappe.db.get_value("Virtual Machine State", asleep, fields),
			("deep-sleeping", "checkpoint-1", "checkpoint-1", requested_at, synced_at),
		)
		self.assertEqual(
			frappe.db.get_value("Virtual Machine State", awake, fields), ("", None, None, None, synced_at)
		)
