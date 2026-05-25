from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_image,
	_ensure_server,
	_new_vm,
)


def _vm_with_status(status: str) -> "frappe.model.document.Document":
	vm = _new_vm()
	vm.status = status
	if status == "Running":
		vm.last_started = frappe.utils.now_datetime()
	elif status == "Stopped":
		vm.last_stopped = frappe.utils.now_datetime()
	vm.save(ignore_permissions=True)
	return vm


class TestVirtualMachineLifecycle(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_server()
		_ensure_image()
		# Clean up VMs from prior tests to free the /124 IPv6 range.
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def test_start_from_stopped_succeeds(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Stopped")
		task = MagicMock()
		task.name = "task-start-1"
		with patch.object(module, "run_task_on_server", return_value=task) as mocked:
			result = vm.start()
		self.assertEqual(result, "task-start-1")
		vm.reload()
		self.assertEqual(vm.status, "Running")
		self.assertIsNotNone(vm.last_started)
		mocked.assert_called_once()
		self.assertEqual(mocked.call_args.kwargs["script"], "start-vm.sh")

	def test_start_from_running_raises(self) -> None:
		vm = _vm_with_status("Running")
		with self.assertRaises(frappe.ValidationError):
			vm.start()

	def test_stop_from_running_succeeds(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		task = MagicMock()
		task.name = "task-stop-1"
		with patch.object(module, "run_task_on_server", return_value=task) as mocked:
			result = vm.stop()
		self.assertEqual(result, "task-stop-1")
		vm.reload()
		self.assertEqual(vm.status, "Stopped")
		self.assertIsNotNone(vm.last_stopped)
		mocked.assert_called_once()
		self.assertEqual(mocked.call_args.kwargs["script"], "stop-vm.sh")

	def test_stop_from_stopped_raises(self) -> None:
		vm = _vm_with_status("Stopped")
		with self.assertRaises(frappe.ValidationError):
			vm.stop()

	def test_restart_from_running_calls_stop_then_start(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		stop_task = MagicMock()
		stop_task.name = "task-stop-r"
		start_task = MagicMock()
		start_task.name = "task-start-r"
		with patch.object(
			module, "run_task_on_server", side_effect=[stop_task, start_task]
		) as mocked:
			result = vm.restart()
		self.assertEqual(result, {"stop_task": "task-stop-r", "start_task": "task-start-r"})
		self.assertEqual(mocked.call_count, 2)
		self.assertEqual(mocked.call_args_list[0].kwargs["script"], "stop-vm.sh")
		self.assertEqual(mocked.call_args_list[1].kwargs["script"], "start-vm.sh")
		vm.reload()
		self.assertEqual(vm.status, "Running")

	def test_restart_from_stopped_only_calls_start(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Stopped")
		start_task = MagicMock()
		start_task.name = "task-start-only"
		with patch.object(
			module, "run_task_on_server", return_value=start_task
		) as mocked:
			result = vm.restart()
		self.assertEqual(result, {"stop_task": None, "start_task": "task-start-only"})
		mocked.assert_called_once()
		self.assertEqual(mocked.call_args.kwargs["script"], "start-vm.sh")
		vm.reload()
		self.assertEqual(vm.status, "Running")

	def test_restart_from_pending_raises(self) -> None:
		vm = _new_vm()  # Pending
		with self.assertRaises(frappe.ValidationError):
			vm.restart()

	def test_delete_vm_succeeds_from_running_archives_row(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		task = MagicMock()
		task.name = "task-del-1"
		with patch.object(module, "run_task_on_server", return_value=task) as mocked:
			result = vm.delete_vm()
		self.assertEqual(result, "task-del-1")
		vm.reload()
		self.assertEqual(vm.status, "Archived")
		mocked.assert_called_once()
		self.assertEqual(mocked.call_args.kwargs["script"], "delete-vm.sh")

	def test_delete_vm_failure_does_not_archive(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		with patch.object(
			module,
			"run_task_on_server",
			side_effect=frappe.ValidationError("delete broke"),
		):
			with self.assertRaises(frappe.ValidationError):
				vm.delete_vm()
		vm.reload()
		self.assertEqual(vm.status, "Running")

	def test_delete_vm_raises_when_already_archived(self) -> None:
		vm = _vm_with_status("Running")
		vm.status = "Archived"
		vm.save(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			vm.delete_vm()

	def test_delete_vm_from_pending_succeeds(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()  # Pending
		task = MagicMock()
		task.name = "task-del-p"
		with patch.object(module, "run_task_on_server", return_value=task):
			vm.delete_vm()
		vm.reload()
		self.assertEqual(vm.status, "Archived")
