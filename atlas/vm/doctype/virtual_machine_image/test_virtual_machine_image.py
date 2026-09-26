import hashlib
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.object_storage import ObjectStorageError
from atlas.vm.core.metal_client import MetalClientError
from atlas.vm.core.multipart_upload import (
	MEBIBYTE,
	MultipartUploadError,
	MultipartUploadService,
	bytes_to_mib,
	get_multipart_part_count,
	get_multipart_part_size_mib,
)
from atlas.vm.core.vm_image_transfer import VirtualMachineImageTransferService
from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import (
	MAXIMUM_SNAPSHOT_VIRTUAL_CPU_COUNT,
	VirtualMachineImage,
)


def artifact_url(image, artifact, expiry_seconds=0) -> str:
	"""Return one predictable artifact URL in place of a signed one."""
	return f"{artifact}-url"


class TestVirtualMachineImage(UnitTestCase):
	def make_image(self, **values):
		defaults = {
			"image_sha256": "a" * 64,
			"kernel_sha256": "b" * 64,
			"artifact_storage": "Object Storage",
			"image_object_key": "images/image/rootfs.img",
			"kernel_object_key": "images/image/kernel",
			"image_file": None,
			"kernel_file": None,
			"image_size_mib": 10,
			"kernel_size_mib": 5,
			"architecture": "amd64",
			"status": "Available",
			"title": "Machine image",
			"cache_image": 0,
			"memory_snapshot": 0,
			"memory_snapshot_virtual_cpu_count": 0,
			"memory_snapshot_memory_mib": 0,
			"memory_snapshot_disk_mib": 0,
			"is_termination_protected": 0,
		}
		defaults.update(values)
		image = object.__new__(VirtualMachineImage)
		for key, value in defaults.items():
			setattr(image, key, value)
		return image

	def test_a_system_image_is_protected_when_it_is_created(self) -> None:
		image = self.make_image(image_type="system")

		image.before_insert()

		self.assertTrue(image.is_termination_protected)

	def test_a_machine_image_keeps_the_requested_protection(self) -> None:
		image = self.make_image(image_type="machine")

		image.before_insert()

		self.assertFalse(image.is_termination_protected)

	def test_a_protected_image_refuses_removal(self) -> None:
		image = self.make_image(is_termination_protected=1)

		with self.assertRaises(frappe.ValidationError):
			image.ensure_not_termination_protected()

	def test_image_record_cannot_be_removed_before_retirement(self) -> None:
		image = self.make_image(status="Archived")
		with self.assertRaises(frappe.ValidationError):
			image.on_trash()

	def test_image_record_cannot_be_removed_with_artifacts(self) -> None:
		image = self.make_image(status="Deleting")
		with self.assertRaises(frappe.ValidationError):
			image.on_trash()

	def test_image_record_cannot_be_removed_while_a_vm_names_it(self) -> None:
		image = self.make_image(
			name="image-1",
			status="Deleting",
			image_object_key=None,
			kernel_object_key=None,
			rootfs_multipart_upload_id=None,
			kernel_multipart_upload_id=None,
			source_server=None,
		)
		with (
			patch(
				"atlas.vm.doctype.virtual_machine_image.virtual_machine_image.frappe.db.exists",
				return_value=True,
			),
			self.assertRaises(frappe.ValidationError),
		):
			image.on_trash()

	def test_clean_image_record_can_be_removed(self) -> None:
		image = self.make_image(
			name="image-1",
			status="Deleting",
			image_object_key=None,
			kernel_object_key=None,
			rootfs_multipart_upload_id=None,
			kernel_multipart_upload_id=None,
			source_server=None,
		)
		with patch(
			"atlas.vm.doctype.virtual_machine_image.virtual_machine_image.frappe.db.exists",
			return_value=False,
		):
			image.on_trash()

	def test_an_archived_image_cannot_boot_a_virtual_machine(self) -> None:
		image = self.make_image(status="Archived")

		with self.assertRaises(frappe.ValidationError):
			image.validate_is_available()

	def test_a_failed_transfer_retries_for_both_image_types(self) -> None:
		for image_type in ("machine", "system"):
			image = self.make_image(
				name="image-1", image_type=image_type, status="Failed", source_server="server-1"
			)
			with (
				patch.object(VirtualMachineImage, "check_permission"),
				patch.object(VirtualMachineImageTransferService, "enqueue") as enqueue,
			):
				image.retry_transfer()

			enqueue.assert_called_once_with("image-1", queue="long", timeout=7200)

	def test_an_image_without_a_source_server_cannot_retry(self) -> None:
		image = self.make_image(name="image-1", image_type="system", status="Failed", source_server=None)

		with (
			patch.object(VirtualMachineImage, "check_permission"),
			self.assertRaises(frappe.ValidationError),
		):
			image.retry_transfer()

	def test_download_returns_only_the_selected_artifact(self) -> None:
		image = self.make_image()
		with (
			patch.object(VirtualMachineImage, "validate_is_available"),
			patch.object(VirtualMachineImage, "get_artifact_url", artifact_url),
		):
			rootfs = image.get_presigned_download_url("rootfs")
			kernel = image.get_presigned_download_url("kernel")

		self.assertEqual(rootfs["artifact"], "rootfs")
		self.assertEqual(rootfs["url"], "rootfs-url")
		self.assertEqual(rootfs["size_mib"], 10)
		self.assertEqual(kernel["artifact"], "kernel")
		self.assertEqual(kernel["url"], "kernel-url")
		self.assertEqual(kernel["size_mib"], 5)

	def test_metal_request_contains_immutable_image_data(self) -> None:
		image = self.make_image()

		with patch.object(VirtualMachineImage, "get_artifact_url", artifact_url):
			request = image.get_metal_image_request()

		identity = f"amd64\0{'a' * 64}\0{'b' * 64}"
		expected_reference = hashlib.sha256(identity.encode()).hexdigest()
		self.assertEqual(request["ref"], f"sha256:{expected_reference}")
		self.assertEqual(request["rootfs"]["sha256"], "a" * 64)
		self.assertEqual(request["kernel"]["sha256"], "b" * 64)

	def test_a_site_file_image_uses_a_public_download_url(self) -> None:
		image = self.make_image(
			artifact_storage="Site File",
			image_object_key=None,
			kernel_object_key=None,
			image_file="file-rootfs",
			kernel_file="file-kernel",
		)

		with patch(
			"atlas.vm.doctype.virtual_machine_image.virtual_machine_image.get_download_url",
			side_effect=lambda name: f"https://atlas.test/files/{name}",
		) as get_download_url:
			self.assertEqual(image.get_artifact_url("rootfs"), "https://atlas.test/files/file-rootfs")
			self.assertEqual(image.get_artifact_url("kernel"), "https://atlas.test/files/file-kernel")

		self.assertEqual(get_download_url.call_count, 2)

	def test_an_unknown_artifact_is_refused(self) -> None:
		image = self.make_image()
		with self.assertRaisesRegex(frappe.ValidationError, "rootfs or kernel"):
			image.get_presigned_download_url("memory")

	def test_a_site_file_image_has_no_signed_download(self) -> None:
		image = self.make_image(
			artifact_storage="Site File", image_file="file-rootfs", kernel_file="file-kernel"
		)

		with self.assertRaises(frappe.ValidationError):
			image.get_presigned_download_url("rootfs")

	def test_only_a_system_image_can_use_site_file_storage(self) -> None:
		image = self.make_image(
			artifact_storage="Site File",
			image_type="machine",
			image_file="file-rootfs",
			kernel_file="file-kernel",
		)

		with self.assertRaises(frappe.ValidationError):
			image.validate_artifacts()

	def test_a_site_file_image_requires_both_files(self) -> None:
		image = self.make_image(
			artifact_storage="Site File", image_type="system", image_file="file-rootfs", kernel_file=None
		)

		with self.assertRaises(frappe.ValidationError):
			image.validate_artifacts()

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

	def test_memory_snapshot_rejects_more_vcpus_than_firecracker_allows(self) -> None:
		image = self.make_image(
			memory_snapshot=1,
			memory_snapshot_virtual_cpu_count=MAXIMUM_SNAPSHOT_VIRTUAL_CPU_COUNT + 1,
			memory_snapshot_memory_mib=2048,
			memory_snapshot_disk_mib=10240,
		)

		with self.assertRaises(frappe.ValidationError):
			image.validate_memory_snapshot_configuration()

		image.memory_snapshot_virtual_cpu_count = MAXIMUM_SNAPSHOT_VIRTUAL_CPU_COUNT
		image.validate_memory_snapshot_configuration()

	def test_memory_without_cache_remains_a_normal_image_request(self) -> None:
		image = self.make_image(
			memory_snapshot=1,
			memory_snapshot_virtual_cpu_count=2,
			memory_snapshot_memory_mib=2048,
			memory_snapshot_disk_mib=10240,
		)
		with (
			patch.object(VirtualMachineImage, "get_artifact_url", artifact_url),
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
			patch.object(VirtualMachineImage, "get_artifact_url", artifact_url),
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
			patch.object(VirtualMachineImage, "get_artifact_url", artifact_url),
		):
			request = image.get_desired_image()

		self.assertTrue(request["cache_image"])
		self.assertTrue(request["memory_snapshot"])
		self.assertEqual(
			request["memory_snapshot_configuration"],
			{"virtual_cpu_count": 2, "memory_mib": 2048, "disk_mib": 10240},
		)


class TestVirtualMachineImageTransfer(UnitTestCase):
	def test_snapshot_uses_the_rounded_up_guest_cpu_count(self) -> None:
		virtual_machine = SimpleNamespace(
			name="VM-00001",
			server="server-1",
			virtual_machine_image="system-image",
			architecture="amd64",
			cpu_millicores=1500,
			memory_mib=2048,
			disk_mib=1024,
			tenant_id=7,
		)
		original_image = SimpleNamespace(
			architecture="amd64",
		)
		server = SimpleNamespace(name="server-1")
		image = Mock()
		image_values = {}
		metal_client = Mock()
		metal_client.create_snapshot.return_value = {
			"id": "01900000-0000-7000-8000-000000000001",
			"rootfs": {"size_bytes": 1024 * 1024},
			"kernel": {"size_bytes": 1024 * 1024},
		}
		service = VirtualMachineImageTransferService()

		def get_doc(doctype, name=None):
			if isinstance(doctype, dict):
				image_values.update(doctype)
				return image
			if doctype == "Virtual Machine Image":
				return original_image
			return server

		with (
			patch("atlas.vm.core.vm_image_transfer.frappe.get_doc", side_effect=get_doc),
			patch("atlas.vm.core.vm_image_transfer.MetalClient", return_value=metal_client),
			patch.object(service, "enqueue") as enqueue_transfer,
		):
			image_name = service.create_from_virtual_machine(
				virtual_machine, "Machine image", memory_snapshot=True
			)

		self.assertEqual(image_name, "01900000-0000-7000-8000-000000000001")
		image.insert.assert_called_once_with(
			set_name="01900000-0000-7000-8000-000000000001",
		)
		self.assertEqual(image_values["memory_snapshot_virtual_cpu_count"], 2)
		self.assertEqual(image_values["memory_snapshot_memory_mib"], 2048)
		enqueue_transfer.assert_called_once_with("01900000-0000-7000-8000-000000000001")

	def test_a_requested_shape_replaces_only_its_own_values(self) -> None:
		virtual_machine = SimpleNamespace(
			name="VM-00001",
			server="server-1",
			virtual_machine_image="system-image",
			architecture="amd64",
			cpu_millicores=1500,
			memory_mib=2048,
			disk_mib=1024,
			tenant_id=0,
		)
		image_values = {}
		metal_client = Mock()
		metal_client.create_snapshot.return_value = {
			"id": "01900000-0000-7000-8000-000000000004",
			"rootfs": {"size_bytes": 1024 * 1024},
			"kernel": {"size_bytes": 1024 * 1024},
		}
		service = VirtualMachineImageTransferService()

		def get_doc(doctype, name=None):
			if isinstance(doctype, dict):
				image_values.update(doctype)
			return Mock()

		with (
			patch("atlas.vm.core.vm_image_transfer.frappe.get_doc", side_effect=get_doc),
			patch("atlas.vm.core.vm_image_transfer.MetalClient", return_value=metal_client),
			patch.object(service, "enqueue"),
		):
			service.create_from_virtual_machine(
				virtual_machine,
				"Machine image",
				memory_snapshot=True,
				memory_snapshot_configuration={"virtual_cpu_count": 8, "memory_mib": 16384},
			)

		self.assertEqual(image_values["memory_snapshot_virtual_cpu_count"], 8)
		self.assertEqual(image_values["memory_snapshot_memory_mib"], 16384)
		self.assertEqual(image_values["memory_snapshot_disk_mib"], 1024)

	def test_requested_tags_reach_the_new_image(self) -> None:
		virtual_machine = SimpleNamespace(
			name="VM-00001",
			server="server-1",
			virtual_machine_image="system-image",
			architecture="amd64",
			disk_mib=1024,
			tenant_id=0,
		)
		values = {}
		metal_client = Mock()
		metal_client.create_snapshot.return_value = {
			"id": "01900000-0000-7000-8000-000000000002",
			"rootfs": {"size_bytes": 1024 * 1024},
			"kernel": {"size_bytes": 1024 * 1024},
		}
		service = VirtualMachineImageTransferService()

		def get_doc(doctype, name=None):
			if isinstance(doctype, dict):
				values.update(doctype)
				return Mock()
			return SimpleNamespace(name="server-1")

		with (
			patch("atlas.vm.core.vm_image_transfer.frappe.get_doc", side_effect=get_doc),
			patch("atlas.vm.core.vm_image_transfer.MetalClient", return_value=metal_client),
			patch.object(service, "enqueue"),
		):
			service.create_from_virtual_machine(
				virtual_machine,
				"Pilot image",
				tags={"purpose": "pilot", "pilot_version": "1.2.3"},
			)

		self.assertEqual(
			values["tags"],
			[{"key": "purpose", "value": "pilot"}, {"key": "pilot_version", "value": "1.2.3"}],
		)

	def test_an_image_without_tags_carries_none(self) -> None:
		virtual_machine = SimpleNamespace(
			name="VM-00001",
			server="server-1",
			virtual_machine_image="system-image",
			architecture="amd64",
			disk_mib=1024,
			tenant_id=7,
		)
		values = {}
		metal_client = Mock()
		metal_client.create_snapshot.return_value = {
			"id": "01900000-0000-7000-8000-000000000003",
			"rootfs": {"size_bytes": 1024 * 1024},
			"kernel": {"size_bytes": 1024 * 1024},
		}
		service = VirtualMachineImageTransferService()

		def get_doc(doctype, name=None):
			if isinstance(doctype, dict):
				values.update(doctype)
				return Mock()
			return SimpleNamespace(name="server-1")

		with (
			patch("atlas.vm.core.vm_image_transfer.frappe.get_doc", side_effect=get_doc),
			patch("atlas.vm.core.vm_image_transfer.MetalClient", return_value=metal_client),
			patch.object(service, "enqueue"),
		):
			service.create_from_virtual_machine(virtual_machine, "Machine image")

		self.assertEqual(values["tags"], [])

	def test_part_count_has_no_empty_boundary_part(self) -> None:
		part_size_mib = get_multipart_part_size_mib(1)
		self.assertEqual(get_multipart_part_count(part_size_mib), 1)
		self.assertEqual(get_multipart_part_count(part_size_mib + 1), 2)

	def test_part_size_grows_with_the_artifact(self) -> None:
		sizes_mib = [1, 600 << 10, 1536 << 10, 5 << 20]
		part_sizes_mib = [get_multipart_part_size_mib(size_mib) for size_mib in sizes_mib]

		self.assertEqual(part_sizes_mib, sorted(set(part_sizes_mib)))
		for size_mib in sizes_mib:
			self.assertLess(get_multipart_part_count(size_mib), 10_000)

	def test_part_count_rejects_an_artifact_over_the_part_limit(self) -> None:
		with self.assertRaises(MultipartUploadError):
			get_multipart_part_count(10 << 20)

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
			image_size_mib=get_multipart_part_size_mib(1) + 1,
			kernel_size_mib=1,
		)
		object_storage_client = Mock()
		object_storage_client.sign_upload_part.side_effect = lambda key, upload_id, part, **kwargs: (
			f"url-{part}"
		)

		request = MultipartUploadService(image, object_storage_client).get_upload_request()

		# The count is an upper bound with one spare part, because the stored
		# artifact is compressed and its part count is not known in advance.
		self.assertEqual([part["part_number"] for part in request["rootfs"]["parts"]], [1, 2, 3])
		self.assertEqual([part["part_number"] for part in request["kernel"]["parts"]], [1, 2])
		self.assertEqual(request["rootfs"]["upload_id"], "rootfs-upload")
		self.assertEqual(request["kernel"]["upload_id"], "kernel-upload")
		self.assertEqual(
			request["rootfs"]["part_size_mib"], get_multipart_part_size_mib(image.image_size_mib)
		)
		self.assertTrue(
			all(
				call.kwargs["expiry_seconds"] == 86400
				for call in object_storage_client.sign_upload_part.call_args_list
			)
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
		settings = SimpleNamespace(get_object_storage_client=Mock(return_value=Mock()))
		service = VirtualMachineImageTransferService()

		with (
			patch("atlas.vm.core.vm_image_transfer.frappe.get_doc", return_value=server),
			patch("atlas.vm.core.vm_image_transfer.frappe.get_single", return_value=settings),
			patch("atlas.vm.core.vm_image_transfer.MetalClient", return_value=metal_client),
			patch.object(MultipartUploadService, "complete_stored_uploads"),
		):
			service.advance(image)

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
			"rootfs": {"sha256": "a" * 64, "stored_size_bytes": 3 * 1024 * 1024},
			"kernel": {"sha256": "b" * 64, "stored_size_bytes": 1024 * 1024},
		}
		settings = SimpleNamespace(get_object_storage_client=Mock(return_value=Mock()))
		service = VirtualMachineImageTransferService()

		with (
			patch(
				"atlas.vm.core.vm_image_transfer.frappe.get_doc",
				return_value=SimpleNamespace(name="server-1"),
			),
			patch("atlas.vm.core.vm_image_transfer.frappe.get_single", return_value=settings),
			patch("atlas.vm.core.vm_image_transfer.MetalClient", return_value=metal_client),
			patch.object(MultipartUploadService, "complete_stored_uploads"),
		):
			# First poll records the checksums; finalize runs on the next poll.
			service.advance(image)
			self.assertEqual(image.image_sha256, "a" * 64)
			self.assertEqual(image.kernel_sha256, "b" * 64)
			self.assertEqual(image.image_stored_size_mib, 3)
			self.assertEqual(image.kernel_stored_size_mib, 1)
			self.assertEqual(image.status, "Completing")
			metal_client.delete_snapshot.assert_not_called()

			# Next poll finalizes, deletes the snapshot, and marks it available.
			service.advance(image)

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
		settings = SimpleNamespace(get_object_storage_client=Mock(return_value=Mock()))
		service = VirtualMachineImageTransferService()

		with (
			patch(
				"atlas.vm.core.vm_image_transfer.frappe.get_doc",
				return_value=SimpleNamespace(name="server-1"),
			),
			patch("atlas.vm.core.vm_image_transfer.frappe.get_single", return_value=settings),
			patch("atlas.vm.core.vm_image_transfer.MetalClient", return_value=metal_client),
			patch.object(service, "start_upload") as start_upload,
		):
			service.advance(image)

		start_upload.assert_called_once()
		metal_client.delete_snapshot.assert_not_called()

	def test_transfer_failure_keeps_identifiers_and_sets_actionable_error(self) -> None:
		image = SimpleNamespace(name="image-1", status="Uploading", transfer_error=None, save=Mock())
		service = VirtualMachineImageTransferService()

		with (
			patch("atlas.vm.core.vm_image_transfer.frappe.get_doc", return_value=image),
			patch("atlas.vm.core.vm_image_transfer.frappe.log_error"),
			patch.object(service, "advance", side_effect=ObjectStorageError("upload failed")),
		):
			service.transfer("image-1")

		self.assertEqual(image.status, "Failed")
		self.assertEqual(image.transfer_error, "upload failed")

	def test_upload_start_error_is_visible_and_keeps_retry_identifiers(self) -> None:
		image = SimpleNamespace(
			name="image-1",
			status="Pending",
			transfer_progress=0,
			transfer_error=None,
			rootfs_multipart_upload_id=None,
			kernel_multipart_upload_id=None,
			image_object_key="images/image/rootfs.img",
			kernel_object_key="images/image/kernel",
			image_size_mib=1,
			kernel_size_mib=1,
			save=Mock(),
		)
		metal_client = Mock()
		metal_client.start_snapshot_upload.side_effect = MetalClientError(
			"upload failed", status=500, code="upload_failed", retryable=True
		)
		object_storage_client = Mock()
		object_storage_client.create_multipart_upload.side_effect = ["rootfs-upload", "kernel-upload"]
		service = VirtualMachineImageTransferService()

		with self.assertRaises(MetalClientError):
			service.start_upload(
				image,
				metal_client,
				MultipartUploadService(image, object_storage_client),
			)

		self.assertEqual(image.rootfs_multipart_upload_id, "rootfs-upload")
		self.assertEqual(image.kernel_multipart_upload_id, "kernel-upload")
		image.save.assert_called_once_with()
