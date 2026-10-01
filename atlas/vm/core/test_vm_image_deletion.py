from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid7

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from atlas.vm.core.metal_client import MetalClientError
from atlas.vm.core.vm_image_deletion import VirtualMachineImageDeletionService, has_virtual_machines

NOW = datetime(2026, 9, 25, 12)
SIX_HOURS = timedelta(hours=6)


def build_image(**overrides) -> SimpleNamespace:
	values = {
		"name": "image-1",
		"image_type": "machine",
		"status": "Available",
		"artifact_storage": "Object Storage",
		"artifact_retention_until": None,
		"site_file_retention_until": None,
		"image_object_key": "images/image-1/rootfs.img",
		"kernel_object_key": "images/image-1/kernel",
		"initrd_object_key": None,
		"image_file": None,
		"kernel_file": None,
		"initrd_file": None,
		"rootfs_multipart_upload_id": "upload-1",
		"kernel_multipart_upload_id": None,
		"initrd_multipart_upload_id": None,
		"source_server": "node-1",
		"save": Mock(),
		"ensure_not_termination_protected": Mock(),
	}
	values.update(overrides)
	return SimpleNamespace(**values)


class TestImageRetirement(UnitTestCase):
	def test_protected_image_keeps_its_status(self) -> None:
		image = build_image(
			ensure_not_termination_protected=Mock(side_effect=frappe.ValidationError("protected"))
		)
		with self.assertRaises(frappe.ValidationError):
			VirtualMachineImageDeletionService().request(image)
		self.assertEqual(image.status, "Available")
		image.save.assert_not_called()

	def test_both_image_types_keep_artifacts_for_six_hours(self) -> None:
		for image_type in ("system", "machine"):
			with self.subTest(image_type=image_type):
				image = build_image(image_type=image_type)
				with patch("atlas.vm.core.vm_image_deletion.now_datetime", return_value=NOW):
					self.assertEqual(VirtualMachineImageDeletionService().request(image), "Archived")
				self.assertEqual(image.artifact_retention_until, NOW + SIX_HOURS)
				self.assertEqual(image.enabled, 0)
				image.save.assert_called_once_with()

	def test_failed_image_can_be_retired(self) -> None:
		image = build_image(status="Failed")
		self.assertEqual(VirtualMachineImageDeletionService().request(image), "Archived")

	def test_incomplete_image_cannot_be_retired(self) -> None:
		for status in ("Pending", "Snapshotting", "Uploading", "Completing", "Cleaning"):
			with self.subTest(status=status), self.assertRaises(frappe.ValidationError):
				VirtualMachineImageDeletionService().request(build_image(status=status))

	def test_archived_image_keeps_its_original_deadline(self) -> None:
		image = build_image(status="Archived", artifact_retention_until=NOW)
		self.assertEqual(VirtualMachineImageDeletionService().request(image), "Archived")
		self.assertEqual(image.artifact_retention_until, NOW)
		image.save.assert_not_called()


class TestArchivedImageReclamation(UnitTestCase):
	def reclaim(self, image, has_virtual_machines=False):
		service = VirtualMachineImageDeletionService()
		database = Mock()
		with (
			patch("atlas.vm.core.vm_image_deletion.frappe.get_all", return_value=[image]) as get_all,
			patch("atlas.vm.core.vm_image_deletion.frappe.db", database),
			patch("atlas.vm.core.vm_image_deletion.now_datetime", return_value=NOW),
			patch("atlas.vm.core.vm_image_deletion.has_virtual_machines", return_value=has_virtual_machines),
			patch.object(service, "enqueue") as enqueue,
		):
			service.reclaim_archived()
		return database, get_all, enqueue

	def test_artifacts_remain_until_retirement_deadline(self) -> None:
		image = build_image(artifact_retention_until=NOW + timedelta(minutes=1))
		database, _get_all, enqueue = self.reclaim(image)
		database.set_value.assert_not_called()
		enqueue.assert_not_called()

	def test_artifacts_are_reclaimed_while_vm_records_exist(self) -> None:
		image = build_image(artifact_retention_until=NOW - timedelta(minutes=1))
		database, get_all, enqueue = self.reclaim(image, has_virtual_machines=True)
		self.assertEqual(get_all.call_args.kwargs["filters"], {"status": "Archived"})
		database.set_value.assert_not_called()
		enqueue.assert_called_once_with("image-1")

	def test_record_waits_for_all_vm_records_to_be_removed(self) -> None:
		image = build_image(
			artifact_retention_until=NOW - timedelta(minutes=1),
			image_object_key=None,
			kernel_object_key=None,
			rootfs_multipart_upload_id=None,
			source_server=None,
		)
		database, _get_all, enqueue = self.reclaim(image, has_virtual_machines=True)
		database.set_value.assert_not_called()
		enqueue.assert_not_called()
		database, _get_all, enqueue = self.reclaim(image)
		database.set_value.assert_called_once_with("Virtual Machine Image", "image-1", "status", "Deleting")
		enqueue.assert_called_once_with("image-1")

	def test_migrated_site_files_keep_their_own_deadline(self) -> None:
		image = build_image(
			artifact_retention_until=NOW - timedelta(minutes=1),
			site_file_retention_until=NOW + timedelta(minutes=1),
		)
		_database, _get_all, enqueue = self.reclaim(image)
		enqueue.assert_not_called()


class TestImageCleanup(UnitTestCase):
	def test_cleanup_waits_for_retirement_deadline(self) -> None:
		image = build_image(artifact_retention_until=NOW + SIX_HOURS)
		with (
			patch("atlas.vm.core.vm_image_deletion.frappe.get_doc", return_value=image),
			patch("atlas.vm.core.vm_image_deletion.now_datetime", return_value=NOW),
			patch("atlas.vm.core.vm_image_deletion.frappe.delete_doc") as delete_doc,
		):
			VirtualMachineImageDeletionService().delete("image-1")
		delete_doc.assert_not_called()

	def test_cleanup_removes_objects_but_keeps_a_referenced_record(self) -> None:
		image = build_image(
			status="Archived",
			artifact_retention_until=NOW - timedelta(minutes=1),
			initrd_object_key="images/image-1/initrd",
			initrd_multipart_upload_id="upload-initrd",
		)
		client = Mock()
		metal_client = Mock()
		with (
			patch("atlas.vm.core.vm_image_deletion.frappe.get_doc", return_value=image),
			patch("atlas.vm.core.vm_image_deletion.now_datetime", return_value=NOW),
			patch("atlas.vm.core.vm_image_deletion.frappe.db", Mock(exists=Mock(return_value=True))),
			patch(
				"atlas.vm.core.vm_image_deletion.frappe.get_single",
				return_value=SimpleNamespace(get_object_storage_client=Mock(return_value=client)),
			),
			patch("atlas.vm.core.vm_image_deletion.MetalClient", return_value=metal_client),
			patch("atlas.vm.core.vm_image_deletion.has_virtual_machines", return_value=True),
			patch("atlas.vm.core.vm_image_deletion.frappe.delete_doc") as delete_doc,
		):
			VirtualMachineImageDeletionService().delete("image-1")
		self.assertEqual(
			[entry.args for entry in client.abort_multipart_upload.call_args_list],
			[
				("images/image-1/rootfs.img", "upload-1"),
				("images/image-1/initrd", "upload-initrd"),
			],
		)
		self.assertEqual(
			[entry.args[0] for entry in client.delete_object.call_args_list],
			["images/image-1/rootfs.img", "images/image-1/kernel", "images/image-1/initrd"],
		)
		metal_client.delete_snapshot.assert_called_once_with("image-1")
		self.assertEqual(image.status, "Archived")
		self.assertIsNone(image.image_object_key)
		self.assertIsNone(image.initrd_object_key)
		self.assertIsNone(image.initrd_multipart_upload_id)
		self.assertIsNone(image.source_server)
		image.save.assert_called_once_with()
		delete_doc.assert_not_called()

	def test_plain_image_keeps_the_two_object_cleanup_path(self) -> None:
		image = build_image(initrd_object_key=None)
		client = Mock()

		VirtualMachineImageDeletionService.delete_objects(image, client)

		self.assertEqual(
			[entry.args[0] for entry in client.delete_object.call_args_list],
			["images/image-1/rootfs.img", "images/image-1/kernel"],
		)

	def test_site_files_are_deleted_without_object_storage(self) -> None:
		image = build_image(
			artifact_storage="Site File",
			image_object_key=None,
			kernel_object_key=None,
			rootfs_multipart_upload_id=None,
			source_server=None,
			image_file="rootfs-file",
			kernel_file="kernel-file",
			initrd_file="initrd-file",
		)
		with (
			patch("atlas.vm.core.vm_image_deletion.frappe.get_doc", return_value=image),
			patch("atlas.vm.core.vm_image_deletion.frappe.get_single") as get_single,
			patch("atlas.vm.core.vm_image_deletion.has_virtual_machines", return_value=True),
			patch("atlas.vm.core.vm_image_deletion.frappe.delete_doc") as delete_doc,
		):
			VirtualMachineImageDeletionService().delete("image-1")
		get_single.assert_not_called()
		self.assertEqual(
			[entry.args[:2] for entry in delete_doc.call_args_list],
			[("File", "rootfs-file"), ("File", "kernel-file"), ("File", "initrd-file")],
		)
		self.assertTrue(all(entry.kwargs["force"] for entry in delete_doc.call_args_list))
		self.assertIsNone(image.image_file)
		self.assertIsNone(image.initrd_file)
		image.save.assert_called_once_with()

	def test_record_is_deleted_after_last_vm_record(self) -> None:
		image = build_image(
			status="Deleting",
			image_object_key=None,
			kernel_object_key=None,
			rootfs_multipart_upload_id=None,
			source_server=None,
		)
		with (
			patch("atlas.vm.core.vm_image_deletion.frappe.get_doc", return_value=image),
			patch("atlas.vm.core.vm_image_deletion.has_virtual_machines", return_value=False),
			patch("atlas.vm.core.vm_image_deletion.frappe.delete_doc") as delete_doc,
		):
			VirtualMachineImageDeletionService().delete("image-1")
		delete_doc.assert_called_once_with("Virtual Machine Image", "image-1")
		image.save.assert_not_called()

	def test_missing_snapshot_does_not_block_cleanup(self) -> None:
		metal_client = Mock()
		metal_client.delete_snapshot.side_effect = MetalClientError("gone", status=404)
		with (
			patch("atlas.vm.core.vm_image_deletion.frappe.db", Mock(exists=Mock(return_value=True))),
			patch("atlas.vm.core.vm_image_deletion.frappe.get_doc", return_value=Mock()),
			patch("atlas.vm.core.vm_image_deletion.MetalClient", return_value=metal_client),
		):
			VirtualMachineImageDeletionService.delete_staged_snapshot(build_image())

	def test_missing_source_server_does_not_block_cleanup(self) -> None:
		metal_client = Mock()
		with (
			patch("atlas.vm.core.vm_image_deletion.frappe.db", Mock(exists=Mock(return_value=False))),
			patch("atlas.vm.core.vm_image_deletion.MetalClient", return_value=metal_client),
		):
			VirtualMachineImageDeletionService.delete_staged_snapshot(build_image())
		metal_client.delete_snapshot.assert_not_called()


class TestImageReferences(IntegrationTestCase):
	def test_terminating_vm_record_keeps_the_image_record(self) -> None:
		image_name = frappe.generate_hash(length=10)
		virtual_machine = frappe.new_doc("Virtual Machine")
		virtual_machine.update(
			{
				"name": frappe.generate_hash(length=10),
				"server": str(uuid7()),
				"virtual_machine_image": image_name,
				"architecture": "amd64",
				"cpu_millicores": 1000,
				"memory_mib": 1024,
				"disk_mib": 10240,
				"tenant_id": 7,
				"is_terminating": 1,
			}
		)
		virtual_machine.db_insert()

		self.assertTrue(has_virtual_machines(image_name))
		frappe.db.delete("Virtual Machine", {"name": virtual_machine.name})
		self.assertFalse(has_virtual_machines(image_name))
