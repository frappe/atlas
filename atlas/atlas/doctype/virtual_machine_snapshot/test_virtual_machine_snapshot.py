from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.virtual_machine.test_virtual_machine import (
	_ensure_test_image,
	_ensure_test_server,
	_new_vm,
)
from atlas.tests._mocks import fake_task


def _stopped_vm() -> "frappe.model.document.Document":
	vm = _new_vm()
	vm.db_set("status", "Stopped")
	vm.reload()
	return vm


def _make_snapshot(vm) -> "frappe.model.document.Document":
	from atlas.atlas.doctype.virtual_machine import virtual_machine as module

	with patch.object(module, "run_task", return_value=fake_task(stdout='ATLAS_RESULT={"size_bytes": 1024}')):
		name = vm.snapshot("snap")
	return frappe.get_doc("Virtual Machine Snapshot", name)


class TestVirtualMachineSnapshot(IntegrationTestCase):
	def setUp(self) -> None:
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		_ensure_test_server()
		_ensure_test_image()
		# Snapshot.on_trash fires a real delete-snapshot-vm.py over SSH for any
		# leftover row whose VM is still live (the test server's 10.0.0.99 is
		# unreachable, so a real call hangs until timeout). Cleanup is harness
		# bookkeeping, not the behaviour under test — stub run_task while we
		# clear prior-test rows.
		with patch.object(module, "run_task", return_value=fake_task()):
			for name in frappe.get_all("Virtual Machine Snapshot", pluck="name"):
				frappe.delete_doc("Virtual Machine Snapshot", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def test_on_trash_runs_delete_script_for_live_vm(self) -> None:
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		snapshot = _make_snapshot(_stopped_vm())
		with patch.object(module, "run_task", return_value=fake_task()) as mocked:
			frappe.delete_doc("Virtual Machine Snapshot", snapshot.name, ignore_permissions=True)
		mocked.assert_called_once()
		self.assertEqual(mocked.call_args.kwargs["script"], "delete-snapshot-vm.py")
		self.assertEqual(mocked.call_args.kwargs["variables"]["SNAPSHOT_ROOTFS_PATH"], snapshot.rootfs_path)

	def test_on_trash_runs_delete_script_for_terminated_vm(self) -> None:
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		# A snapshot LV lives in the thin pool, OUTSIDE the VM directory that
		# terminate-vm.py rm -rf'd — so it survives terminate and on_trash MUST
		# still lvremove it, even for a Terminated VM. (The old file-backed model
		# could skip this because the files were already gone with the directory.)
		vm = _stopped_vm()
		snapshot = _make_snapshot(vm)
		vm.db_set("status", "Terminated")
		with patch.object(module, "run_task", return_value=fake_task()) as mocked:
			frappe.delete_doc("Virtual Machine Snapshot", snapshot.name, ignore_permissions=True)
		mocked.assert_called_once()
		self.assertEqual(mocked.call_args.kwargs["script"], "delete-snapshot-vm.py")

	def test_clone_to_new_vm_creates_fresh_identity(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module

		source = _stopped_vm()
		snapshot = _make_snapshot(source)

		# Don't let the enqueued auto_provision run in-process.
		with patch.object(vm_module.frappe, "enqueue"):
			clone_name = snapshot.clone_to_new_vm(title="cloned vm", ssh_public_key="ssh-ed25519 CLONE")

		clone = frappe.get_doc("Virtual Machine", clone_name)
		self.assertNotEqual(clone.name, source.name)
		self.assertNotEqual(clone.ipv6_address, source.ipv6_address)
		self.assertNotEqual(clone.mac_address, source.mac_address)
		self.assertEqual(clone.server, source.server)
		self.assertEqual(clone.image, snapshot.source_image)
		self.assertEqual(clone.clone_source_rootfs, snapshot.rootfs_path)
		self.assertEqual(clone.ssh_public_key, "ssh-ed25519 CLONE")
		self.assertEqual(clone.status, "Pending")

	def test_clone_provision_variables_carry_snapshot_path(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module

		source = _stopped_vm()
		snapshot = _make_snapshot(source)
		with patch.object(vm_module.frappe, "enqueue"):
			clone_name = snapshot.clone_to_new_vm(title="cloned vm 2", ssh_public_key="ssh-ed25519 CLONE2")
		clone = frappe.get_doc("Virtual Machine", clone_name)
		variables = clone._provision_variables()
		self.assertEqual(variables["SNAPSHOT_ROOTFS_PATH"], snapshot.rootfs_path)
		# Kernel still comes from the image.
		self.assertEqual(variables["IMAGE_NAME"], clone.image)

	def test_clone_disk_cannot_shrink_below_snapshot(self) -> None:
		source = _stopped_vm()
		snapshot = _make_snapshot(source)
		# Snapshot captured disk_gigabytes from the source VM (2 in fixtures).
		with self.assertRaises(frappe.ValidationError) as raised:
			snapshot.clone_to_new_vm(title="too small", ssh_public_key="ssh-ed25519 X", disk_gigabytes=1)
		self.assertIn("cannot be smaller", str(raised.exception))

	def test_clone_rejects_unavailable_snapshot(self) -> None:
		source = _stopped_vm()
		snapshot = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "pending",
				"virtual_machine": source.name,
				"server": source.server,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError) as raised:
			snapshot.clone_to_new_vm(title="x", ssh_public_key="ssh-ed25519 X")
		self.assertIn("not Available", str(raised.exception))

	def test_clone_carries_data_disk(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module

		source = _new_vm(data_disk_gigabytes=2, data_disk_format_and_mount=1, data_disk_mount_point="/home")
		source.db_set("status", "Stopped")
		source.reload()
		with patch.object(
			vm_module,
			"run_task",
			return_value=fake_task(stdout='ATLAS_RESULT={"size_bytes": 1024, "data_size_bytes": 2048}'),
		):
			snapshot = frappe.get_doc("Virtual Machine Snapshot", source.snapshot("snap-data"))

		with patch.object(vm_module.frappe, "enqueue"):
			clone = frappe.get_doc(
				"Virtual Machine",
				snapshot.clone_to_new_vm(title="clone-with-data", ssh_public_key="ssh-ed25519 C"),
			)
		# The clone inherits the data disk's size + mount config and seeds it from
		# the snapshot's data half.
		self.assertEqual(clone.data_disk_gigabytes, 2)
		self.assertEqual(clone.data_disk_mount_point, "/home")
		self.assertEqual(clone.clone_source_data_rootfs, snapshot.data_rootfs_path)
		variables = clone._provision_variables()
		self.assertEqual(variables["DATA_SNAPSHOT_ROOTFS_PATH"], snapshot.data_rootfs_path)
		self.assertEqual(variables["DATA_DISK_GB"], "2")

	def test_on_trash_removes_data_snapshot_lv(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		source = _new_vm(data_disk_gigabytes=2)
		source.db_set("status", "Stopped")
		source.reload()
		with patch.object(
			vm_module,
			"run_task",
			return_value=fake_task(stdout='ATLAS_RESULT={"size_bytes": 1, "data_size_bytes": 2}'),
		):
			snapshot = frappe.get_doc("Virtual Machine Snapshot", source.snapshot("doomed-data"))

		with patch.object(module, "run_task", return_value=fake_task()) as mocked:
			frappe.delete_doc("Virtual Machine Snapshot", snapshot.name, ignore_permissions=True)
		self.assertEqual(
			mocked.call_args.kwargs["variables"]["DATA_SNAPSHOT_ROOTFS_PATH"], snapshot.data_rootfs_path
		)

	def test_on_trash_skips_when_no_rootfs_path(self) -> None:
		from atlas.atlas.doctype.virtual_machine_snapshot import virtual_machine_snapshot as module

		vm = _stopped_vm()
		snapshot = frappe.get_doc(
			{
				"doctype": "Virtual Machine Snapshot",
				"title": "incomplete",
				"virtual_machine": vm.name,
				"server": vm.server,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)
		with patch.object(module, "run_task") as mocked:
			frappe.delete_doc("Virtual Machine Snapshot", snapshot.name, ignore_permissions=True)
		mocked.assert_not_called()
