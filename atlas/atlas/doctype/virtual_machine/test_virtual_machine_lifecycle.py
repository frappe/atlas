from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.tests._mocks import fake_task

# What a successful snapshot-stop-vm.py prints; stop() parses it back. Tests
# that exercise stop() on its default (memory-snapshot) path reuse this.
_SNAPSHOT_STOP_STDOUT = (
	'ATLAS_RESULT={"memory_snapshot": true, "reason": "", "memory_snapshot_bytes": 536870912}'
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
		self.assertEqual(mocked.call_args.kwargs["script"], "start-vm.py")

	def test_start_from_running_raises(self) -> None:
		vm = _vm_with_status("Running")
		with self.assertRaises(frappe.ValidationError):
			vm.start()

	def test_stop_from_running_succeeds(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		# stop() defaults to the memory-snapshot fast path (memory_snapshot_on_stop
		# is on by default) and parses the typed result back.
		task = fake_task(name="task-stop-1", stdout=_SNAPSHOT_STOP_STDOUT)
		with patch.object(module, "run_task", return_value=task) as mocked:
			result = vm.stop()
		self.assertEqual(result, "task-stop-1")
		vm.reload()
		self.assertEqual(vm.status, "Stopped")
		self.assertIsNotNone(vm.last_stopped)
		self.assertTrue(vm.has_memory_snapshot)
		mocked.assert_called_once()
		self.assertEqual(mocked.call_args.kwargs["script"], "snapshot-stop-vm.py")

	def test_stop_from_stopped_raises(self) -> None:
		vm = _vm_with_status("Stopped")
		with self.assertRaises(frappe.ValidationError):
			vm.stop()

	def test_restart_from_running_calls_stop_then_start(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		stop_task = fake_task(name="task-stop-r", stdout=_SNAPSHOT_STOP_STDOUT)
		start_task = fake_task(name="task-start-r")
		with patch.object(module, "run_task", side_effect=[stop_task, start_task]) as mocked:
			result = vm.restart()
		self.assertEqual(result, {"stop_task": "task-stop-r", "start_task": "task-start-r"})
		self.assertEqual(mocked.call_count, 2)
		self.assertEqual(mocked.call_args_list[0].kwargs["script"], "snapshot-stop-vm.py")
		self.assertEqual(mocked.call_args_list[1].kwargs["script"], "start-vm.py")
		vm.reload()
		self.assertEqual(vm.status, "Running")

	def test_restart_from_stopped_only_calls_start(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Stopped")
		start_task = fake_task(name="task-start-only")
		with patch.object(module, "run_task", return_value=start_task) as mocked:
			result = vm.restart()
		self.assertEqual(result, {"stop_task": None, "start_task": "task-start-only"})
		mocked.assert_called_once()
		self.assertEqual(mocked.call_args.kwargs["script"], "start-vm.py")
		vm.reload()
		self.assertEqual(vm.status, "Running")

	def test_restart_from_pending_raises(self) -> None:
		vm = _new_vm()  # Pending
		with self.assertRaises(frappe.ValidationError):
			vm.restart()

	def test_rebuild_from_image_runs_script(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Stopped")
		task = fake_task(name="task-rebuild-img")
		with patch.object(module, "run_task", return_value=task) as mocked:
			result = vm.rebuild("image")
		self.assertEqual(result, "task-rebuild-img")
		self.assertEqual(mocked.call_args.kwargs["script"], "rebuild-vm.py")
		variables = mocked.call_args.kwargs["variables"]
		self.assertEqual(variables["IMAGE_NAME"], _ensure_test_image())
		self.assertNotIn("SNAPSHOT_ROOTFS_PATH", variables)
		# VM stays Stopped after rebuild.
		vm.reload()
		self.assertEqual(vm.status, "Stopped")

	def test_rebuild_from_snapshot_runs_script(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Stopped")
		snapshot = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "snap",
				"virtual_machine": vm.name,
				"server": vm.server,
				"status": "Available",
				"rootfs_path": "/dev/atlas/atlas-snap-s1",
			}
		).insert(ignore_permissions=True)
		task = fake_task(name="task-rebuild-snap")
		with patch.object(module, "run_task", return_value=task) as mocked:
			result = vm.rebuild("snapshot", snapshot.name)
		self.assertEqual(result, "task-rebuild-snap")
		variables = mocked.call_args.kwargs["variables"]
		self.assertEqual(variables["SNAPSHOT_ROOTFS_PATH"], snapshot.rootfs_path)
		self.assertNotIn("IMAGE_NAME", variables)

	def test_rebuild_rejects_when_not_stopped(self) -> None:
		vm = _vm_with_status("Running")
		with self.assertRaises(frappe.ValidationError) as raised:
			vm.rebuild("image")
		self.assertIn("Stop the VM before rebuilding", str(raised.exception))

	def test_rebuild_snapshot_of_other_vm_rejected(self) -> None:
		vm = _vm_with_status("Stopped")
		other = _new_vm()
		snapshot = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "foreign",
				"virtual_machine": other.name,
				"server": other.server,
				"status": "Available",
				"rootfs_path": "/dev/atlas/atlas-snap-foreign",
			}
		).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as raised:
			vm.rebuild("snapshot", snapshot.name)
		self.assertIn("different Virtual Machine", str(raised.exception))

	def test_rebuild_unavailable_snapshot_rejected(self) -> None:
		vm = _vm_with_status("Stopped")
		snapshot = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "pending-snap",
				"virtual_machine": vm.name,
				"server": vm.server,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as raised:
			vm.rebuild("snapshot", snapshot.name)
		self.assertIn("not Available", str(raised.exception))

	def test_rebuild_unknown_source_type_rejected(self) -> None:
		vm = _vm_with_status("Stopped")
		with self.assertRaises(frappe.ValidationError) as raised:
			vm.rebuild("banana")
		self.assertIn("Unknown rebuild source_type", str(raised.exception))

	def test_rebuild_snapshot_without_source_rejected(self) -> None:
		vm = _vm_with_status("Stopped")
		with self.assertRaises(frappe.ValidationError) as raised:
			vm.rebuild("snapshot")
		self.assertIn("requires a snapshot", str(raised.exception))

	def test_pause_from_running_succeeds(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		task = fake_task(name="task-pause-1")
		with patch.object(module, "run_task", return_value=task) as mocked:
			result = vm.pause()
		self.assertEqual(result, "task-pause-1")
		self.assertEqual(mocked.call_args.kwargs["script"], "pause-vm.py")
		vm.reload()
		self.assertEqual(vm.status, "Paused")

	def test_pause_from_stopped_raises(self) -> None:
		vm = _vm_with_status("Stopped")
		with self.assertRaises(frappe.ValidationError):
			vm.pause()

	def test_resume_from_paused_succeeds(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		vm.status = "Paused"
		vm.save(ignore_permissions=True)
		task = fake_task(name="task-resume-1")
		with patch.object(module, "run_task", return_value=task) as mocked:
			result = vm.resume()
		self.assertEqual(result, "task-resume-1")
		self.assertEqual(mocked.call_args.kwargs["script"], "resume-vm.py")
		vm.reload()
		self.assertEqual(vm.status, "Running")

	def test_resume_from_running_raises(self) -> None:
		vm = _vm_with_status("Running")
		with self.assertRaises(frappe.ValidationError):
			vm.resume()

	def test_stop_from_paused_succeeds(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		vm.status = "Paused"
		vm.save(ignore_permissions=True)
		with patch.object(module, "run_task", return_value=fake_task(name="task-stop-p", stdout=_SNAPSHOT_STOP_STDOUT)):
			vm.stop()
		vm.reload()
		self.assertEqual(vm.status, "Stopped")

	def test_terminate_from_paused_succeeds(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		vm.status = "Paused"
		vm.save(ignore_permissions=True)
		with patch.object(module, "run_task", return_value=fake_task(name="task-term-p")):
			vm.terminate()
		vm.reload()
		self.assertEqual(vm.status, "Terminated")

	def test_restart_from_paused_raises(self) -> None:
		vm = _vm_with_status("Running")
		vm.status = "Paused"
		vm.save(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			vm.restart()

	def test_start_from_paused_raises(self) -> None:
		vm = _vm_with_status("Running")
		vm.status = "Paused"
		vm.save(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			vm.start()

	def test_resize_from_stopped_persists_and_runs_script(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Stopped")
		task = fake_task(name="task-resize-1")
		with patch.object(module, "run_task", return_value=task) as mocked:
			result = vm.resize(vcpus=4, memory_megabytes=4096, disk_gigabytes=20)
		self.assertEqual(result, "task-resize-1")
		self.assertEqual(mocked.call_args.kwargs["script"], "resize-vm.py")
		variables = mocked.call_args.kwargs["variables"]
		self.assertEqual(variables["VCPUS"], "4")
		self.assertEqual(variables["MEMORY_MB"], "4096")
		self.assertEqual(variables["DISK_GB"], "20")
		vm.reload()
		self.assertEqual(vm.vcpus, 4)
		self.assertEqual(vm.memory_megabytes, 4096)
		self.assertEqual(vm.disk_gigabytes, 20)
		self.assertEqual(vm.status, "Stopped")

	def test_resize_defaults_unspecified_fields(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Stopped")
		before_memory = vm.memory_megabytes
		before_disk = vm.disk_gigabytes
		with patch.object(module, "run_task", return_value=fake_task()):
			vm.resize(vcpus=2)
		vm.reload()
		self.assertEqual(vm.vcpus, 2)
		self.assertEqual(vm.memory_megabytes, before_memory)
		self.assertEqual(vm.disk_gigabytes, before_disk)

	def test_resize_rejects_when_not_stopped(self) -> None:
		vm = _vm_with_status("Running")
		with self.assertRaises(frappe.ValidationError) as raised:
			vm.resize(vcpus=2)
		self.assertIn("Stop the VM before resizing", str(raised.exception))

	def test_resize_rejects_disk_shrink(self) -> None:
		vm = _vm_with_status("Stopped")
		# fixtures default disk is 2 GB; ask for 1.
		with self.assertRaises(frappe.ValidationError) as raised:
			vm.resize(disk_gigabytes=1)
		self.assertIn("can only grow", str(raised.exception))

	def test_resize_keeps_whole_core_vm_whole_core(self) -> None:
		# A whole-core VM (cpu_max_cores == vcpus) that resizes vcpus without an
		# explicit cap tracks the new vcpus, so it stays whole-core.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Stopped")  # vcpus=1, cpu_max_cores=1
		with patch.object(module, "run_task", return_value=fake_task()):
			vm.resize(vcpus=4)
		vm.reload()
		self.assertEqual(vm.vcpus, 4)
		self.assertEqual(vm.cpu_max_cores, 4.0)

	def test_resize_accepts_explicit_fractional_cap(self) -> None:
		# An explicit cpu_max_cores wins: persist a fractional cap on a Stopped VM.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Stopped")
		with patch.object(module, "run_task", return_value=fake_task()):
			vm.resize(cpu_max_cores=0.5)
		vm.reload()
		self.assertEqual(vm.cpu_max_cores, 0.5)
		self.assertEqual(vm.vcpus, 1, "vcpus unchanged when only the cap is resized")

	def test_snapshot_defaults_title_when_omitted(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Stopped")
		vm.db_set("title", "web-01")
		task = fake_task(stdout='ATLAS_RESULT={"size_bytes": 123}')
		with patch.object(module, "run_task", return_value=task):
			snapshot_name = vm.snapshot()  # no title
		title = frappe.db.get_value("Virtual Machine Snapshot", snapshot_name, "title")
		self.assertTrue(title.startswith("web-01 — "), title)

	def test_snapshot_uses_given_title(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Stopped")
		task = fake_task(stdout='ATLAS_RESULT={"size_bytes": 123}')
		with patch.object(module, "run_task", return_value=task):
			snapshot_name = vm.snapshot("nightly")
		title = frappe.db.get_value("Virtual Machine Snapshot", snapshot_name, "title")
		self.assertEqual(title, "nightly")

	def test_ordinary_save_of_resource_field_still_blocked(self) -> None:
		# The drift guard must stay live: only resize() may move these fields.
		vm = _vm_with_status("Stopped")
		vm.vcpus = 8
		with self.assertRaises(frappe.ValidationError) as raised:
			vm.save(ignore_permissions=True)
		self.assertIn("vcpus is immutable", str(raised.exception))

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
		self.assertEqual(mocked.call_args.kwargs["script"], "terminate-vm.py")

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

	def test_stop_protection_blocks_stop(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		vm.stop_protection = 1
		vm.save(ignore_permissions=True)
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(frappe.ValidationError) as raised:
				vm.stop()
		self.assertIn("stop protection", str(raised.exception))
		mocked.assert_not_called()  # no Task runs when the gate trips
		vm.reload()
		self.assertEqual(vm.status, "Running")

	def test_stop_protection_blocks_restart(self) -> None:
		# restart() stops first, so stop protection blocks it too.
		vm = _vm_with_status("Running")
		vm.stop_protection = 1
		vm.save(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as raised:
			vm.restart()
		self.assertIn("stop protection", str(raised.exception))

	def test_stop_protection_does_not_block_terminate(self) -> None:
		# terminate() does not route through stop(); the gates are independent.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		vm.stop_protection = 1
		vm.save(ignore_permissions=True)
		with patch.object(module, "run_task", return_value=fake_task(name="task-term-sp")):
			vm.terminate()
		vm.reload()
		self.assertEqual(vm.status, "Terminated")

	def test_termination_protection_blocks_terminate(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		vm.termination_protection = 1
		vm.save(ignore_permissions=True)
		with patch.object(module, "run_task") as mocked:
			with self.assertRaises(frappe.ValidationError) as raised:
				vm.terminate()
		self.assertIn("termination protection", str(raised.exception))
		mocked.assert_not_called()
		vm.reload()
		self.assertEqual(vm.status, "Running")

	def test_termination_protection_does_not_block_stop(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		vm.termination_protection = 1
		vm.save(ignore_permissions=True)
		with patch.object(module, "run_task", return_value=fake_task(name="task-stop-tp", stdout=_SNAPSHOT_STOP_STDOUT)):
			vm.stop()
		vm.reload()
		self.assertEqual(vm.status, "Stopped")

	def test_terminate_after_clearing_protection_succeeds(self) -> None:
		# The two-step path: uncheck + save, then terminate.
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _vm_with_status("Running")
		vm.termination_protection = 1
		vm.save(ignore_permissions=True)
		vm.termination_protection = 0
		vm.save(ignore_permissions=True)
		with patch.object(module, "run_task", return_value=fake_task(name="task-term-cleared")):
			vm.terminate()
		vm.reload()
		self.assertEqual(vm.status, "Terminated")
