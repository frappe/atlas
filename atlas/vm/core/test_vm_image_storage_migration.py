from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from atlas.atlas.object_storage import ObjectStorageError
from atlas.vm.core import vm_image_storage_migration
from atlas.vm.core.vm_image_storage_migration import (
	SITE_FILE_RETENTION,
	VirtualMachineImageStorageMigration,
	delete_expired_site_files,
	enqueue_site_file_image_migrations,
)

NOW = datetime(2026, 9, 16, 10, 0)


def fixed_clock():
	"""Pin the module clock, so a retention time is exact and reads no site setting."""
	return patch.object(vm_image_storage_migration, "now_datetime", return_value=NOW)


def build_image(**overrides) -> SimpleNamespace:
	"""Return one bootstrap System image that site files hold."""
	values = {
		"name": "image-1",
		"image_type": "system",
		"status": "Available",
		"artifact_storage": "Site File",
		"is_stored_in_site_file": True,
		"image_file": "file-rootfs",
		"kernel_file": "file-kernel",
		"initrd_file": None,
		"image_object_key": None,
		"kernel_object_key": None,
		"initrd_object_key": None,
		"image_sha256": "a" * 64,
		"kernel_sha256": "b" * 64,
		"initrd_sha256": None,
		"has_initrd": False,
		"site_file_retention_until": None,
		"save": Mock(),
	}
	values.update(overrides)
	return SimpleNamespace(**values)


class TestVirtualMachineImageStorageMigrationRequest(UnitTestCase):
	def test_an_object_storage_image_cannot_be_migrated(self) -> None:
		image = build_image(artifact_storage="Object Storage", is_stored_in_site_file=False)

		with self.assertRaises(frappe.ValidationError):
			VirtualMachineImageStorageMigration().request(image)

	def test_an_incomplete_image_cannot_be_migrated(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			VirtualMachineImageStorageMigration().request(build_image(status="Pending"))

	def test_a_request_queues_the_migration(self) -> None:
		migration = VirtualMachineImageStorageMigration()

		with patch.object(migration, "enqueue") as enqueue:
			migration.request(build_image())

		enqueue.assert_called_once_with("image-1")


class TestVirtualMachineImageStorageMigration(UnitTestCase):
	def test_migration_uploads_and_keeps_the_files_for_their_retention_time(self) -> None:
		image = build_image()
		client = Mock()

		with (
			patch(
				"atlas.vm.core.vm_image_storage_migration.frappe.get_doc",
				side_effect=[
					image,
					SimpleNamespace(get_full_path=lambda: "/site/files/aaaaaaaaaaaa-rootfs.ext4"),
					SimpleNamespace(get_full_path=lambda: "/site/files/bbbbbbbbbbbb-kernel"),
				],
			),
			patch(
				"atlas.vm.core.vm_image_storage_migration.frappe.get_single",
				return_value=SimpleNamespace(get_object_storage_client=lambda: client),
			),
			patch.object(Path, "stat", return_value=SimpleNamespace(st_size=1024)),
			patch.object(VirtualMachineImageStorageMigration, "validate_stored_size") as validate_stored_size,
			patch("atlas.vm.core.vm_image_storage_migration.frappe.db", Mock()),
			patch("atlas.vm.core.vm_image_storage_migration.frappe.delete_doc") as delete_doc,
			fixed_clock(),
		):
			VirtualMachineImageStorageMigration().migrate("image-1")

		self.assertEqual(image.image_object_key, "images/image-1/rootfs.img")
		self.assertEqual(image.kernel_object_key, "images/image-1/kernel")
		self.assertIsNone(image.initrd_object_key)
		self.assertEqual(image.artifact_storage, "Object Storage")
		self.assertEqual(image.image_file, "file-rootfs")
		self.assertEqual(image.kernel_file, "file-kernel")
		self.assertEqual(image.site_file_retention_until, NOW + SITE_FILE_RETENTION)
		image.save.assert_called_once()
		self.assertEqual(validate_stored_size.call_count, 2)
		delete_doc.assert_not_called()

	def test_migration_uploads_an_initrd_when_present(self) -> None:
		image = build_image(
			has_initrd=True,
			initrd_file="file-initrd",
			initrd_sha256="c" * 64,
		)
		client = Mock()

		with (
			patch(
				"atlas.vm.core.vm_image_storage_migration.frappe.get_doc",
				side_effect=[
					image,
					SimpleNamespace(get_full_path=lambda: "/site/files/aaaaaaaaaaaa-rootfs.ext4"),
					SimpleNamespace(get_full_path=lambda: "/site/files/bbbbbbbbbbbb-kernel"),
					SimpleNamespace(get_full_path=lambda: "/site/files/cccccccccccc-initrd"),
				],
			),
			patch(
				"atlas.vm.core.vm_image_storage_migration.frappe.get_single",
				return_value=SimpleNamespace(get_object_storage_client=lambda: client),
			),
			patch.object(Path, "stat", return_value=SimpleNamespace(st_size=1024)),
			patch.object(VirtualMachineImageStorageMigration, "validate_stored_size") as validate_stored_size,
			patch("atlas.vm.core.vm_image_storage_migration.frappe.db", Mock()),
			patch("atlas.vm.core.vm_image_storage_migration.frappe.delete_doc"),
			fixed_clock(),
		):
			VirtualMachineImageStorageMigration().migrate("image-1")

		self.assertEqual(image.initrd_object_key, "images/image-1/initrd")
		self.assertEqual(
			[call.args[1] for call in client.upload_file.call_args_list],
			["images/image-1/rootfs.img", "images/image-1/kernel", "images/image-1/initrd"],
		)
		self.assertEqual(validate_stored_size.call_count, 3)

	def test_a_migrated_image_is_left_alone(self) -> None:
		image = build_image(artifact_storage="Object Storage", is_stored_in_site_file=False)

		with (
			patch("atlas.vm.core.vm_image_storage_migration.frappe.get_doc", return_value=image),
			patch("atlas.vm.core.vm_image_storage_migration.frappe.get_single") as get_single,
		):
			VirtualMachineImageStorageMigration().migrate("image-1")

		get_single.assert_not_called()
		image.save.assert_not_called()

	def test_a_short_stored_object_fails_the_migration(self) -> None:
		client = Mock(head_object=Mock(return_value={"ContentLength": 10}))

		with self.assertRaises(ObjectStorageError):
			VirtualMachineImageStorageMigration.validate_stored_size(client, "key", 20)

	def test_a_missing_stored_object_fails_the_migration(self) -> None:
		client = Mock(head_object=Mock(return_value=None))

		with self.assertRaises(ObjectStorageError):
			VirtualMachineImageStorageMigration.validate_stored_size(client, "key", 20)


class TestSiteFileImageMigrationTrigger(UnitTestCase):
	def test_nothing_is_queued_without_object_storage(self) -> None:
		settings = SimpleNamespace(is_object_storage_configured=False)

		with (
			patch("atlas.vm.core.vm_image_storage_migration.frappe.get_single", return_value=settings),
			patch("atlas.vm.core.vm_image_storage_migration.frappe.get_all") as get_all,
		):
			enqueue_site_file_image_migrations()

		get_all.assert_not_called()

	def test_every_available_site_file_image_is_queued(self) -> None:
		settings = SimpleNamespace(is_object_storage_configured=True)

		with (
			patch("atlas.vm.core.vm_image_storage_migration.frappe.get_single", return_value=settings),
			patch(
				"atlas.vm.core.vm_image_storage_migration.frappe.get_all",
				return_value=["image-1", "image-2"],
			) as get_all,
			patch.object(VirtualMachineImageStorageMigration, "enqueue") as enqueue,
		):
			enqueue_site_file_image_migrations()

		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{"artifact_storage": "Site File", "status": "Available"},
		)
		self.assertEqual([call.args[0] for call in enqueue.call_args_list], ["image-1", "image-2"])


class TestExpiredSiteFileRemoval(UnitTestCase):
	def delete(self, image: SimpleNamespace) -> Mock:
		"""Run one site file cleanup against a stubbed database."""
		with (
			patch("atlas.vm.core.vm_image_storage_migration.frappe.get_doc", return_value=image),
			patch("atlas.vm.core.vm_image_storage_migration.frappe.db", Mock()),
			patch("atlas.vm.core.vm_image_storage_migration.frappe.delete_doc") as delete_doc,
		):
			VirtualMachineImageStorageMigration().delete_site_files("image-1")

		return delete_doc

	def test_the_files_go_before_the_record_drops_its_retention_time(self) -> None:
		image = build_image(
			artifact_storage="Object Storage",
			is_stored_in_site_file=False,
			site_file_retention_until="2026-09-16 10:00:00",
			initrd_file="file-initrd",
		)

		delete_doc = self.delete(image)

		self.assertIsNone(image.image_file)
		self.assertIsNone(image.kernel_file)
		self.assertIsNone(image.initrd_file)
		self.assertIsNone(image.site_file_retention_until)
		image.save.assert_called_once()
		self.assertEqual(
			[call.args[1] for call in delete_doc.call_args_list],
			["file-rootfs", "file-kernel", "file-initrd"],
		)
		self.assertTrue(all(call.kwargs["force"] for call in delete_doc.call_args_list))

	def test_a_failed_delete_leaves_the_image_for_the_next_sweep(self) -> None:
		image = build_image(
			artifact_storage="Object Storage",
			is_stored_in_site_file=False,
			site_file_retention_until="2026-09-16 10:00:00",
		)

		with (
			patch("atlas.vm.core.vm_image_storage_migration.frappe.get_doc", return_value=image),
			patch("atlas.vm.core.vm_image_storage_migration.frappe.db", Mock()),
			patch(
				"atlas.vm.core.vm_image_storage_migration.frappe.delete_doc",
				side_effect=OSError("storage is down"),
			),
			self.assertRaises(OSError),
		):
			VirtualMachineImageStorageMigration().delete_site_files("image-1")

		self.assertEqual(image.image_file, "file-rootfs")
		self.assertEqual(image.site_file_retention_until, "2026-09-16 10:00:00")
		image.save.assert_not_called()

	def test_a_site_file_image_keeps_its_files(self) -> None:
		image = build_image()

		delete_doc = self.delete(image)

		self.assertEqual(image.image_file, "file-rootfs")
		image.save.assert_not_called()
		delete_doc.assert_not_called()


class TestExpiredSiteFileSweep(IntegrationTestCase):
	def insert_image(self, name: str, artifact_storage: str, site_file_retention_until: str | None) -> None:
		image = frappe.get_doc(
			{
				"doctype": "Virtual Machine Image",
				"name": name,
				"title": name,
				"image_type": "system",
				"status": "Available",
				"artifact_storage": artifact_storage,
				"site_file_retention_until": site_file_retention_until,
			}
		)
		image.db_insert()

	def test_only_images_past_their_retention_time_are_swept(self) -> None:
		self.insert_image("sweep-expired", "Object Storage", "2026-09-16 09:00:00")
		self.insert_image("sweep-retained", "Object Storage", "2026-09-16 11:00:00")
		self.insert_image("sweep-no-retention", "Object Storage", None)
		self.insert_image("sweep-site-file", "Site File", "2026-09-16 09:00:00")

		with (
			patch.object(VirtualMachineImageStorageMigration, "delete_site_files") as delete_site_files,
			fixed_clock(),
		):
			delete_expired_site_files()

		delete_site_files.assert_called_once_with("sweep-expired")
