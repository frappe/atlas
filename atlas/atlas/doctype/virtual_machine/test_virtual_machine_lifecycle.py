from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.tests._mocks import fake_task


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
		_ensure_test_server()
		_ensure_test_image()
		# Clean up VMs from prior tests to free the /124 IPv6 range.
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def test_start_from_stopped_succeeds(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Stopped")
		task = fake_task(name="task-start-1")
		with patch.object(module, "run_task", return_value=task) as mocked:
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
		task = fake_task(name="task-stop-1")
		with patch.object(module, "run_task", return_value=task) as mocked:
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
		stop_task = fake_task(name="task-stop-r")
		start_task = fake_task(name="task-start-r")
		with patch.object(
			module, "run_task", side_effect=[stop_task, start_task]
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
		start_task = fake_task(name="task-start-only")
		with patch.object(
			module, "run_task", return_value=start_task
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

	def test_terminate_succeeds_from_running_marks_row(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		task = fake_task(name="task-del-1")
		with patch.object(module, "run_task", return_value=task) as mocked:
			result = vm.terminate()
		self.assertEqual(result, "task-del-1")
		vm.reload()
		self.assertEqual(vm.status, "Terminated")
		mocked.assert_called_once()
		self.assertEqual(mocked.call_args.kwargs["script"], "terminate-vm.sh")

	def test_terminate_failure_does_not_mark(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		with patch.object(
			module,
			"run_task",
			side_effect=frappe.ValidationError("terminate broke"),
		):
			with self.assertRaises(frappe.ValidationError):
				vm.terminate()
		vm.reload()
		self.assertEqual(vm.status, "Running")

	def test_terminate_raises_when_already_terminated(self) -> None:
		vm = _vm_with_status("Running")
		vm.status = "Terminated"
		vm.save(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			vm.terminate()

	def test_terminate_from_pending_succeeds(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()  # Pending
		task = fake_task(name="task-del-p")
		with patch.object(module, "run_task", return_value=task):
			vm.terminate()
		vm.reload()
		self.assertEqual(vm.status, "Terminated")
