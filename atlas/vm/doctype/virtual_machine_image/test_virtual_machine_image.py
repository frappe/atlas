import hashlib
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.s3 import S3Error
from atlas.vm.core.virtual_machine_image_manager import (
	MEBIBYTE,
	MULTIPART_PART_SIZE_MIB,
	VirtualMachineImageManager,
	bytes_to_mib,
	get_multipart_part_count,
)
from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage


class TestVirtualMachineImage(UnitTestCase):
	def make_image(self, **values):
		defaults = {
			"image_sha256": "a" * 64,
			"kernel_sha256": "b" * 64,
			"image_object_key": "images/image/rootfs.img",
			"kernel_object_key": "images/image/kernel",
			"image_size_mib": 10,
			"kernel_size_mib": 5,
			"platform": "amd64",
			"status": "Available",
			"title": "Machine image",
			"supports_cloud_init": 1,
			"cache_image": 0,
			"memory_snapshot": 0,
			"memory_snapshot_virtual_cpu_count": 0,
			"memory_snapshot_memory_mib": 0,
			"memory_snapshot_disk_mib": 0,
		}
		defaults.update(values)
		image = object.__new__(VirtualMachineImage)
		for key, value in defaults.items():
			setattr(image, key, value)
		return image

	def test_metal_request_contains_immutable_image_data(self) -> None:
		image = self.make_image()

		with (
			patch.object(VirtualMachineImage, "validate_user_data"),
			patch.object(VirtualMachineImage, "get_image_url", return_value="https://example.test/image"),
			patch.object(VirtualMachineImage, "get_kernel_url", return_value="https://example.test/kernel"),
		):
			request = image.get_metal_image_request("#cloud-config")

		identity = f"amd64\0{'a' * 64}\0{'b' * 64}"
		expected_reference = hashlib.sha256(identity.encode()).hexdigest()
		self.assertEqual(request["ref"], f"sha256:{expected_reference}")
		self.assertEqual(request["rootfs"]["sha256"], "a" * 64)
		self.assertEqual(request["kernel"]["sha256"], "b" * 64)

	def test_image_requires_disk_to_hold_rootfs(self) -> None:
		image = self.make_image(image_size_mib=10240)

		image.validate_compatibility(disk_mib=10240)
		image.validate_compatibility(disk_mib=20480)
		with self.assertRaises(frappe.ValidationError):
			image.validate_compatibility(disk_mib=1024)

	def test_memory_snapshot_image_allows_any_matching_disk(self) -> None:
		# Metal cold boots on a spec mismatch, so a memory snapshot image only needs
		# a disk that holds its root file system.
		image = self.make_image(
			image_size_mib=10240,
			memory_snapshot=1,
			memory_snapshot_virtual_cpu_count=2,
			memory_snapshot_memory_mib=2048,
			memory_snapshot_disk_mib=10240,
		)

		image.validate_compatibility(disk_mib=20480)
		with self.assertRaises(frappe.ValidationError):
			image.validate_compatibility(disk_mib=1024)

	def test_memory_snapshot_requires_positive_configuration(self) -> None:
		image = self.make_image(
			memory_snapshot=1,
			memory_snapshot_virtual_cpu_count=2,
			memory_snapshot_memory_mib=0,
			memory_snapshot_disk_mib=1024,
		)

		with self.assertRaises(frappe.ValidationError):
			image.validate_memory_snapshot_configuration()

	def test_memory_without_cache_remains_a_normal_image_request(self) -> None:
		image = self.make_image(
			memory_snapshot=1,
			memory_snapshot_virtual_cpu_count=2,
			memory_snapshot_memory_mib=2048,
			memory_snapshot_disk_mib=10240,
		)
		with (
			patch.object(VirtualMachineImage, "get_image_url", return_value="rootfs"),
			patch.object(VirtualMachineImage, "get_kernel_url", return_value="kernel"),
		):
			request = image.get_metal_image_request()

		self.assertNotIn("cache_image", request)
		self.assertNotIn("memory_snapshot", request)

	def test_cached_vm_request_contains_memory_policy(self) -> None:
		image = self.make_image(
			cache_image=1,
			memory_snapshot=1,
			memory_snapshot_virtual_cpu_count=2,
			memory_snapshot_memory_mib=2048,
			memory_snapshot_disk_mib=10240,
		)
		with (
			patch.object(VirtualMachineImage, "get_image_url", return_value="rootfs"),
			patch.object(VirtualMachineImage, "get_kernel_url", return_value="kernel"),
		):
			request = image.get_metal_image_request()

		self.assertTrue(request["cache_image"])
		self.assertTrue(request["memory_snapshot"])

	def test_desired_image_contains_memory_policy(self) -> None:
		image = self.make_image(
			cache_image=1,
			memory_snapshot=1,
			memory_snapshot_virtual_cpu_count=2,
			memory_snapshot_memory_mib=2048,
			memory_snapshot_disk_mib=10240,
		)
		with (
			patch.object(VirtualMachineImage, "get_image_url", return_value="rootfs"),
			patch.object(VirtualMachineImage, "get_kernel_url", return_value="kernel"),
		):
			request = image.get_desired_image()

		self.assertTrue(request["cache_image"])
		self.assertTrue(request["memory_snapshot"])
		self.assertEqual(
			request["memory_snapshot_configuration"],
			{"virtual_cpu_count": 2, "memory_mib": 2048, "disk_mib": 10240},
		)


class TestVirtualMachineImageTransfer(UnitTestCase):
	def test_snapshot_uuid_becomes_machine_image_name(self) -> None:
		virtual_machine = SimpleNamespace(
			name="VM-00001",
			server="server-1",
			virtual_machine_image="system-image",
			disk_mib=1024,
		)
		original_image = SimpleNamespace(
			platform="amd64",
			operating_system="Ubuntu",
			operating_system_version="24.04",
			supports_cloud_init=1,
		)
		server = SimpleNamespace(name="server-1")
		image = Mock()
		metal_client = Mock()
		metal_client.create_snapshot.return_value = {
			"id": "01900000-0000-7000-8000-000000000001",
			"rootfs": {"size_bytes": 1024 * 1024},
			"kernel": {"size_bytes": 1024 * 1024},
		}
		manager = VirtualMachineImageManager()

		def get_doc(doctype, name=None):
			if isinstance(doctype, dict):
				return image
			if doctype == "Virtual Machine Image":
				return original_image
			return server

		with (
			patch("atlas.vm.core.virtual_machine_image_manager.frappe.get_doc", side_effect=get_doc),
			patch("atlas.vm.core.virtual_machine_image_manager.MetalClient", return_value=metal_client),
			patch.object(manager, "enqueue_transfer") as enqueue_transfer,
		):
			image_name = manager.create_from_virtual_machine(virtual_machine, "Machine image")

		self.assertEqual(image_name, "01900000-0000-7000-8000-000000000001")
		image.insert.assert_called_once_with(
			ignore_permissions=True,
			set_name="01900000-0000-7000-8000-000000000001",
		)
		enqueue_transfer.assert_called_once_with("01900000-0000-7000-8000-000000000001")

	def test_part_count_has_no_empty_boundary_part(self) -> None:
		self.assertEqual(get_multipart_part_count(MULTIPART_PART_SIZE_MIB), 1)
		self.assertEqual(get_multipart_part_count(MULTIPART_PART_SIZE_MIB + 1), 2)

	def test_bytes_round_up_to_whole_mib(self) -> None:
		self.assertEqual(bytes_to_mib(1), 1)
		self.assertEqual(bytes_to_mib(MEBIBYTE), 1)
		self.assertEqual(bytes_to_mib(MEBIBYTE + 1), 2)

	def test_upload_request_signs_each_part_for_one_day(self) -> None:
		image = SimpleNamespace(
			image_object_key="images/image/rootfs.img",
			kernel_object_key="images/image/kernel",
			rootfs_multipart_upload_id="rootfs-upload",
			kernel_multipart_upload_id="kernel-upload",
			image_size_mib=MULTIPART_PART_SIZE_MIB + 1,
			kernel_size_mib=1,
		)
		s3_client = Mock()
		s3_client.sign_upload_part.side_effect = lambda key, upload_id, part, **kwargs: f"url-{part}"

		request = VirtualMachineImageManager().get_upload_request(image, s3_client)

		self.assertEqual([part["part_number"] for part in request["rootfs"]["parts"]], [1, 2])
		self.assertEqual([part["part_number"] for part in request["kernel"]["parts"]], [1])
		self.assertTrue(
			all(call.kwargs["expiry_seconds"] == 86400 for call in s3_client.sign_upload_part.call_args_list)
		)

	def test_successful_transfer_marks_image_available_and_deletes_staging(self) -> None:
		image = SimpleNamespace(
			name="image-1",
			image_sha256="a" * 64,
			kernel_sha256="b" * 64,
			source_server="server-1",
			status="Failed",
			transfer_error="old error",
			save=Mock(),
		)
		server = SimpleNamespace(name="server-1")
		metal_client = Mock()
		settings = SimpleNamespace(get_s3_client=Mock(return_value=Mock()))
		manager = VirtualMachineImageManager()

		with (
			patch("atlas.vm.core.virtual_machine_image_manager.frappe.get_doc", return_value=server),
			patch("atlas.vm.core.virtual_machine_image_manager.frappe.get_single", return_value=settings),
			patch("atlas.vm.core.virtual_machine_image_manager.MetalClient", return_value=metal_client),
			patch("atlas.vm.core.virtual_machine_image_manager.frappe.db.commit"),
			patch.object(manager, "complete_stored_uploads"),
		):
			manager.advance_transfer(image)

		metal_client.delete_snapshot.assert_called_once_with("image-1")
		self.assertEqual(image.status, "Available")
		self.assertIsNone(image.transfer_error)

	def test_completed_upload_status_records_sha256_and_finalizes(self) -> None:
		image = SimpleNamespace(
			name="image-1",
			image_sha256=None,
			kernel_sha256=None,
			source_server="server-1",
			status="Uploading",
			transfer_error=None,
			save=Mock(),
		)
		metal_client = Mock()
		metal_client.get_snapshot.return_value = {
			"state": "completed",
			"rootfs": {"sha256": "a" * 64},
			"kernel": {"sha256": "b" * 64},
		}
		settings = SimpleNamespace(get_s3_client=Mock(return_value=Mock()))
		manager = VirtualMachineImageManager()

		with (
			patch(
				"atlas.vm.core.virtual_machine_image_manager.frappe.get_doc",
				return_value=SimpleNamespace(name="server-1"),
			),
			patch("atlas.vm.core.virtual_machine_image_manager.frappe.get_single", return_value=settings),
			patch("atlas.vm.core.virtual_machine_image_manager.MetalClient", return_value=metal_client),
			patch("atlas.vm.core.virtual_machine_image_manager.frappe.db.commit"),
			patch.object(manager, "complete_stored_uploads"),
		):
			manager.advance_transfer(image)

		self.assertEqual(image.image_sha256, "a" * 64)
		self.assertEqual(image.kernel_sha256, "b" * 64)
		metal_client.delete_snapshot.assert_called_once_with("image-1")
		self.assertEqual(image.status, "Available")

	def test_pending_upload_status_starts_the_upload(self) -> None:
		image = SimpleNamespace(
			name="image-1",
			image_sha256=None,
			kernel_sha256=None,
			source_server="server-1",
		)
		metal_client = Mock()
		metal_client.get_snapshot.return_value = {"state": "pending"}
		settings = SimpleNamespace(get_s3_client=Mock(return_value=Mock()))
		manager = VirtualMachineImageManager()

		with (
			patch(
				"atlas.vm.core.virtual_machine_image_manager.frappe.get_doc",
				return_value=SimpleNamespace(name="server-1"),
			),
			patch("atlas.vm.core.virtual_machine_image_manager.frappe.get_single", return_value=settings),
			patch("atlas.vm.core.virtual_machine_image_manager.MetalClient", return_value=metal_client),
			patch.object(manager, "start_upload") as start_upload,
		):
			manager.advance_transfer(image)

		start_upload.assert_called_once()
		metal_client.delete_snapshot.assert_not_called()

	def test_transfer_failure_keeps_identifiers_and_sets_actionable_error(self) -> None:
		image = SimpleNamespace(name="image-1", status="Uploading", transfer_error=None, save=Mock())
		manager = VirtualMachineImageManager()

		with (
			patch("atlas.vm.core.virtual_machine_image_manager.frappe.get_doc", return_value=image),
			patch("atlas.vm.core.virtual_machine_image_manager.frappe.db.commit"),
			patch("atlas.vm.core.virtual_machine_image_manager.frappe.log_error"),
			patch.object(manager, "advance_transfer", side_effect=S3Error("upload failed")),
		):
			manager.transfer("image-1")

		self.assertEqual(image.status, "Failed")
		self.assertEqual(image.transfer_error, "upload failed")
