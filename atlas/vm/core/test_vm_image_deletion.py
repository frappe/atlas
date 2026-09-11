from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.vm.core.metal_client import MetalClientError
from atlas.vm.core.vm_image_deletion import VirtualMachineImageDeletionService


def build_image(**overrides) -> SimpleNamespace:
	"""Return one Machine image that a transfer left behind."""
	values = {
		"name": "image-1",
		"image_type": "machine",
		"status": "Available",
		"image_object_key": "images/image-1/rootfs.img",
		"kernel_object_key": "images/image-1/kernel",
		"rootfs_multipart_upload_id": "upload-1",
		"kernel_multipart_upload_id": None,
		"source_server": "node-1",
		"save": Mock(),
	}
	values.update(overrides)
	return SimpleNamespace(**values)


class TestMachineImageDeletionRequest(UnitTestCase):
	def test_a_system_image_cannot_be_deleted(self) -> None:
		service = VirtualMachineImageDeletionService()

		with self.assertRaises(frappe.ValidationError):
			service.request(build_image(image_type="system"))

	def test_an_image_in_use_cannot_be_deleted(self) -> None:
		service = VirtualMachineImageDeletionService()

		with (
			patch("atlas.vm.core.vm_image_deletion.frappe.db", Mock(exists=Mock(return_value=True))),
			self.assertRaises(frappe.ValidationError),
		):
			service.request(build_image())

	def test_an_incomplete_image_cannot_be_deleted(self) -> None:
		service = VirtualMachineImageDeletionService()

		for status in ("Pending", "Uploading", "Failed", "Deleting"):
			with self.subTest(status=status), self.assertRaises(frappe.ValidationError):
				service.request(build_image(status=status))

	def test_a_request_marks_the_image_and_queues_the_cleanup(self) -> None:
		service = VirtualMachineImageDeletionService()
		image = build_image()

		with (
			patch("atlas.vm.core.vm_image_deletion.frappe.db", Mock(exists=Mock(return_value=False))),
			patch.object(service, "enqueue") as enqueue,
		):
			service.request(image)

		self.assertEqual(image.status, "Deleting")
		self.assertEqual(image.enabled, 0)
		image.save.assert_called_once_with()
		enqueue.assert_called_once_with("image-1")


class TestMachineImageCleanup(UnitTestCase):
	def test_cleanup_removes_uploads_objects_and_staged_data(self) -> None:
		service = VirtualMachineImageDeletionService()
		image = build_image()
		client = Mock()
		metal_client = Mock()

		with (
			patch("atlas.vm.core.vm_image_deletion.frappe.get_doc", return_value=image),
			patch("atlas.vm.core.vm_image_deletion.frappe.db", Mock(exists=Mock(return_value=True))),
			patch(
				"atlas.vm.core.vm_image_deletion.frappe.get_single",
				return_value=SimpleNamespace(get_object_storage_client=Mock(return_value=client)),
			),
			patch("atlas.vm.core.vm_image_deletion.MetalClient", return_value=metal_client),
			patch("atlas.vm.core.vm_image_deletion.frappe.delete_doc") as delete_doc,
			patch.object(service, "validate_is_unused"),
		):
			service.delete("image-1")

		client.abort_multipart_upload.assert_called_once_with("images/image-1/rootfs.img", "upload-1")
		self.assertEqual(client.delete_object.call_count, 2)
		metal_client.delete_snapshot.assert_called_once_with("image-1")
		delete_doc.assert_called_once()

	def test_an_absent_snapshot_does_not_stop_the_cleanup(self) -> None:
		metal_client = Mock()
		metal_client.delete_snapshot.side_effect = MetalClientError("gone", status=404)

		with (
			patch("atlas.vm.core.vm_image_deletion.frappe.db", Mock(exists=Mock(return_value=True))),
			patch("atlas.vm.core.vm_image_deletion.frappe.get_doc", return_value=Mock()),
			patch("atlas.vm.core.vm_image_deletion.MetalClient", return_value=metal_client),
		):
			VirtualMachineImageDeletionService.delete_staged_snapshot(build_image())

	def test_an_absent_source_server_is_skipped(self) -> None:
		metal_client = Mock()

		with (
			patch("atlas.vm.core.vm_image_deletion.frappe.db", Mock(exists=Mock(return_value=False))),
			patch("atlas.vm.core.vm_image_deletion.MetalClient", return_value=metal_client),
		):
			VirtualMachineImageDeletionService.delete_staged_snapshot(build_image())

		metal_client.delete_snapshot.assert_not_called()
