from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.vm.core import reconciliation
from atlas.vm.core.metal_client import MetalClientError


def metal_error(status: int) -> MetalClientError:
	return MetalClientError("metal said no", status=status)


class TestReconciliation(UnitTestCase):
	def setUp(self) -> None:
		frappe.cache.delete_value(reconciliation.failure_key("VM-00001", "draft reconciliation"))

	def settle(self, *, side_effect=None, on_present=None, delete_error=None) -> MagicMock:
		"""Run one settle pass against a stubbed Metal client and record the document."""
		virtual_machine = MagicMock()
		virtual_machine.name = "VM-00001"
		virtual_machine.delete.side_effect = delete_error
		client = MagicMock()
		client.get_virtual_machine.side_effect = side_effect

		with (
			patch.object(reconciliation.frappe, "get_doc", return_value=virtual_machine),
			patch.object(reconciliation, "VirtualMachineService") as service,
			patch.object(reconciliation.frappe, "log_error") as log_error,
			patch.object(reconciliation.frappe, "get_traceback", return_value=""),
			patch.object(reconciliation.frappe, "db") as database,
		):
			service.return_value.metal_client = client
			reconciliation.settle("VM-00001", "draft reconciliation", on_present)

		virtual_machine.log_error = log_error
		virtual_machine.database = database
		return virtual_machine

	def test_a_confirmed_absence_deletes_the_record(self) -> None:
		virtual_machine = self.settle(side_effect=metal_error(404))

		virtual_machine.delete.assert_called_once_with()
		self.assertTrue(virtual_machine.flags.metal_absence_confirmed)
		virtual_machine.database.commit.assert_called_once()

	def test_a_blocked_delete_is_logged_and_does_not_stop_the_batch(self) -> None:
		"""A blocked delete must not stop the records that follow."""
		virtual_machine = self.settle(side_effect=metal_error(404), delete_error=frappe.LinkExistsError)

		virtual_machine.database.rollback.assert_called_once()
		virtual_machine.database.commit.assert_not_called()
		virtual_machine.log_error.assert_called_once()

	def test_an_unreachable_host_keeps_the_record(self) -> None:
		"""A host Atlas cannot reach says nothing about whether the VM exists."""
		virtual_machine = self.settle(side_effect=metal_error(503))

		virtual_machine.delete.assert_not_called()
		virtual_machine.log_error.assert_called_once()

		# Every pass fails while the host is down, so later passes do not log again.
		self.settle(side_effect=metal_error(503)).log_error.assert_not_called()

	def test_a_present_virtual_machine_runs_the_present_action(self) -> None:
		seen: list[object] = []
		virtual_machine = self.settle(on_present=seen.append)

		virtual_machine.delete.assert_not_called()
		self.assertEqual(seen, [virtual_machine])

	def test_a_present_virtual_machine_without_an_action_is_left_alone(self) -> None:
		virtual_machine = self.settle(on_present=None)

		virtual_machine.delete.assert_not_called()
		virtual_machine.db_set.assert_not_called()

	def test_stale_draft_reconciliation_clears_the_draft_flag(self) -> None:
		virtual_machine = MagicMock()

		with (
			patch.object(reconciliation.frappe, "get_doc", return_value=virtual_machine),
			patch.object(reconciliation, "VirtualMachineService"),
		):
			reconciliation.reconcile_stale_draft("VM-00001")

		virtual_machine.db_set.assert_called_once_with("is_draft", 0)

	def test_terminating_reconciliation_leaves_a_present_virtual_machine(self) -> None:
		virtual_machine = MagicMock()

		with (
			patch.object(reconciliation.frappe, "get_doc", return_value=virtual_machine),
			patch.object(reconciliation, "VirtualMachineService"),
		):
			reconciliation.reconcile_terminating("VM-00001")

		virtual_machine.db_set.assert_not_called()
		virtual_machine.delete.assert_not_called()
