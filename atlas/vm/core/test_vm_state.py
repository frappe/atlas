from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import get_datetime

from atlas.vm.core import vm_state


class TestVirtualMachineState(UnitTestCase):
	def store(
		self,
		names: list[str],
		stored: list[str] | dict[str, str],
		reported: dict,
		save_error: Exception | None = None,
	) -> dict:
		"""Store one reported state set against a stubbed database.

		`stored` names the rows that already exist. A list stores them with a status the
		report never repeats, so each one counts as a change; a dict pins the stored
		status, which is how an unchanged report is set up.
		"""
		if isinstance(stored, list):
			stored = dict.fromkeys(stored, "__unreported__")
		rows = [frappe._dict(name=name, status=status) for name, status in stored.items()]
		saved = []

		def make_document(*args) -> MagicMock:
			document = MagicMock()
			document.save.side_effect = save_error
			document.insert.side_effect = save_error
			saved.append(document)
			return document

		with (
			patch.object(vm_state, "now_datetime", return_value=datetime(2026, 9, 9, 10, 0)),
			patch.object(vm_state.frappe, "get_all", side_effect=[names, rows]),
			patch.object(vm_state.frappe, "get_doc", side_effect=make_document),
			patch.object(vm_state.frappe, "new_doc", side_effect=make_document),
			patch.object(vm_state.frappe, "log_error"),
			patch.object(vm_state.frappe.db, "set_value") as set_value,
			patch.object(vm_state.frappe.db, "commit") as commit,
		):
			vm_state.store_reported_states("metal-1", reported)

		return {"saved": saved, "commit": commit, "set_value": set_value}

	def test_a_first_report_inserts_and_a_changed_report_updates(self) -> None:
		calls = self.store(
			names=["vm-00001", "vm-00002"],
			stored={"vm-00002": "stopped"},
			reported={"vm-00001": {"status": "running"}, "vm-00002": {"status": "running"}},
		)

		inserted, updated = calls["saved"]
		self.assertEqual(inserted.virtual_machine, "vm-00001")
		inserted.insert.assert_called_once()
		self.assertEqual(updated.status, "running")
		self.assertEqual(updated.synced_at, datetime(2026, 9, 9, 10, 0))

	def test_every_changed_virtual_machine_is_saved_as_a_document(self) -> None:
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

	def test_an_unchanged_report_stamps_the_time_without_a_document_event(self) -> None:
		"""A host reports on a timer. Saving the document on every repeat fires `on_update`,
		and a Webhook on that event then delivers one call per virtual machine per report."""
		calls = self.store(
			names=["vm-00001", "vm-00002"],
			stored={"vm-00001": "running", "vm-00002": "running"},
			reported={"vm-00001": {"status": "running"}, "vm-00002": {"status": "running"}},
		)

		self.assertEqual(calls["saved"], [])
		self.assertEqual(calls["set_value"].call_count, 2)
		self.assertEqual(
			calls["set_value"].call_args.args,
			("Virtual Machine State", "vm-00002", "synced_at", datetime(2026, 9, 9, 10, 0)),
		)
		self.assertFalse(calls["set_value"].call_args.kwargs["update_modified"])

	def test_a_changed_status_still_saves_the_document(self) -> None:
		calls = self.store(
			names=["vm-00001"],
			stored={"vm-00001": "running"},
			reported={"vm-00001": {"status": "stopped"}},
		)

		self.assertEqual([document.status for document in calls["saved"]], ["stopped"])
		self.assertEqual(calls["set_value"].call_count, 0)

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


class TestReportedStateWrites(IntegrationTestCase):
	"""What a report writes, against the database rather than a stub.

	A Webhook on Virtual Machine State delivers on `on_update`, and a host reports every
	few seconds, so whether an unchanged report writes the document decides whether that
	Webhook fires forever. `modified` is the observable proof: only a document write moves
	it.
	"""

	def setUp(self) -> None:
		# The writer commits each row. Hold the test transaction open so it still rolls back.
		self.enterContext(patch.object(vm_state.frappe.db, "commit"))
		self.server = frappe.generate_hash(length=10)
		self.virtual_machine = frappe.generate_hash(length=10)
		machine = frappe.new_doc("Virtual Machine")
		machine.update(
			{
				"name": self.virtual_machine,
				"server": self.server,
				"virtual_machine_image": frappe.generate_hash(length=10),
				"architecture": "amd64",
				"cpu_millicores": 1000,
				"memory_mib": 1024,
				"disk_mib": 10240,
				"tenant_id": 7,
			}
		)
		machine.db_insert()

	def report(self, status: str) -> None:
		vm_state.store_reported_states(self.server, {self.virtual_machine: {"status": status}})

	def stored(self, field: str):
		return frappe.db.get_value("Virtual Machine State", self.virtual_machine, field)

	def freeze_modified(self) -> datetime:
		"""Park `modified` in the past, so a later write is unmistakable."""
		frozen = datetime(2020, 1, 1)
		frappe.db.set_value(
			"Virtual Machine State", self.virtual_machine, "modified", frozen, update_modified=False
		)
		return frozen

	def test_a_first_report_writes_the_document(self) -> None:
		self.report("running")

		self.assertEqual(self.stored("status"), "running")
		self.assertIsNotNone(self.stored("synced_at"))

	def test_an_unchanged_report_does_not_write_the_document(self) -> None:
		self.report("running")
		frozen = self.freeze_modified()

		self.report("running")

		self.assertEqual(get_datetime(self.stored("modified")), frozen)
		self.assertEqual(self.stored("status"), "running")

	def test_an_unchanged_report_still_stamps_the_time(self) -> None:
		self.report("running")
		first = self.stored("synced_at")
		with patch.object(vm_state, "now_datetime", return_value=datetime(2030, 1, 1, 12, 0)):
			self.report("running")

		self.assertNotEqual(self.stored("synced_at"), first)
		self.assertEqual(self.stored("synced_at"), datetime(2030, 1, 1, 12, 0))

	def test_a_changed_report_writes_the_document(self) -> None:
		self.report("running")
		frozen = self.freeze_modified()

		self.report("stopped")

		self.assertNotEqual(get_datetime(self.stored("modified")), frozen)
		self.assertEqual(self.stored("status"), "stopped")


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
				"server": "metal-test",
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
