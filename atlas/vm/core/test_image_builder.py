from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from atlas.vm.core.image_builder import (
	build_ubuntu_image,
	publish_to_site_files,
	publish_ubuntu_image,
	publish_ubuntu_image_variant,
	upload_to_object_storage,
)
from atlas.vm.core.multipart_upload import MEBIBYTE


class TestUbuntuImageBuilder(UnitTestCase):
	def test_build_uses_the_virtual_machine_image_script_and_complete_names(self) -> None:
		with (
			TemporaryDirectory() as temporary_directory,
			patch("atlas.vm.core.image_builder.IS_MACOS", False),
			patch("atlas.vm.core.image_builder.os.geteuid", return_value=1000),
			patch("atlas.vm.core.image_builder.subprocess.run") as run,
		):
			image_path, kernel_path, initrd_path = build_ubuntu_image(
				"24.04", "amd64", True, Path(temporary_directory)
			)

		command = run.call_args.args[0]
		self.assertEqual(command[0], "sudo")
		self.assertTrue(str(command[1]).endswith("vm/scripts/build_ubuntu_server_image.sh"))
		self.assertIn("--minimal", command)
		self.assertEqual(image_path.name, "ubuntu-24.04-minimal-amd64.ext4")
		self.assertEqual(kernel_path.name, "vmlinux-ubuntu-24.04-minimal-server")
		self.assertEqual(initrd_path.name, "initrd-ubuntu-24.04-minimal-server")
		self.assertEqual(command[command.index("--initrd-output") + 1], initrd_path)
		run.assert_called_once_with(command, check=True)

	def test_variant_keeps_an_unchanged_image_record(self) -> None:
		existing = SimpleNamespace(image_sha256="a" * 64, kernel_sha256="b" * 64, initrd_sha256=None)
		with (
			patch("atlas.vm.core.image_builder.get_available_ubuntu_images", return_value=[existing]),
			patch("atlas.vm.core.image_builder.frappe.get_single") as get_single,
			patch("atlas.vm.core.image_builder.VirtualMachineImageDeletionService") as deletion,
		):
			publish_ubuntu_image_variant(
				title="Ubuntu 24.04",
				version="24.04",
				architecture="amd64",
				image_path=Path("rootfs.img"),
				image_sha256="a" * 64,
				kernel_path=Path("kernel"),
				kernel_sha256="b" * 64,
				storage="Object Storage",
			)

		get_single.assert_not_called()
		deletion.assert_not_called()

	def test_publish_adds_a_new_record_and_retires_the_previous_one(self) -> None:
		previous = SimpleNamespace(image_sha256="c" * 64, kernel_sha256="b" * 64, is_termination_protected=1)
		created = {}

		with (
			patch("atlas.vm.core.image_builder.get_available_ubuntu_images", return_value=[previous]),
			patch("atlas.vm.core.image_builder.frappe.generate_hash", return_value="new-image"),
			patch(
				"atlas.vm.core.image_builder.upload_to_object_storage",
				return_value={"image_object_key": "rootfs-key", "kernel_object_key": "kernel-key"},
			) as upload,
			patch("atlas.vm.core.image_builder.Path.stat", return_value=SimpleNamespace(st_size=MEBIBYTE)),
			patch(
				"atlas.vm.core.image_builder.frappe.get_doc",
				side_effect=lambda values: SimpleNamespace(
					insert=lambda **kwargs: created.update(values | kwargs)
				),
			),
			patch("atlas.vm.core.image_builder.VirtualMachineImageDeletionService") as deletion,
		):
			publish_ubuntu_image_variant(
				title="Ubuntu 24.04",
				version="24.04",
				architecture="amd64",
				image_path=Path("rootfs.img"),
				image_sha256="a" * 64,
				kernel_path=Path("kernel"),
				kernel_sha256="b" * 64,
				storage="Object Storage",
			)

		self.assertEqual(created["set_name"], "new-image")
		self.assertNotIn("version", created)
		self.assertEqual(created["image_object_key"], "rootfs-key")
		upload.assert_called_once_with("new-image", Path("rootfs.img.zst"), Path("kernel"), None)
		self.assertEqual(created["image_stored_size_mib"], 1)
		deletion.return_value.request.assert_called_once_with(previous)
		self.assertEqual(previous.is_termination_protected, 0)

	def test_encryption_variant_records_its_initrd(self) -> None:
		created = {}
		with (
			patch("atlas.vm.core.image_builder.get_available_ubuntu_images", return_value=[]),
			patch("atlas.vm.core.image_builder.frappe.generate_hash", return_value="encrypted-image"),
			patch(
				"atlas.vm.core.image_builder.upload_to_object_storage",
				return_value={
					"image_object_key": "rootfs-key",
					"kernel_object_key": "kernel-key",
					"initrd_object_key": "initrd-key",
				},
			) as upload,
			patch("atlas.vm.core.image_builder.Path.stat", return_value=SimpleNamespace(st_size=MEBIBYTE)),
			patch(
				"atlas.vm.core.image_builder.frappe.get_doc",
				side_effect=lambda values: SimpleNamespace(
					insert=lambda **kwargs: created.update(values | kwargs)
				),
			),
		):
			publish_ubuntu_image_variant(
				title="Ubuntu 24.04 (Disk Encryption)",
				version="24.04",
				architecture="amd64",
				image_path=Path("rootfs.img"),
				image_sha256="a" * 64,
				kernel_path=Path("kernel"),
				kernel_sha256="b" * 64,
				storage="Object Storage",
				initrd_path=Path("initrd"),
				initrd_sha256="c" * 64,
			)

		upload.assert_called_once_with(
			"encrypted-image", Path("rootfs.img.zst"), Path("kernel"), Path("initrd")
		)
		self.assertEqual(created["initrd_object_key"], "initrd-key")
		self.assertEqual(created["initrd_sha256"], "c" * 64)
		self.assertEqual(created["initrd_size_mib"], 1)
		self.assertEqual(
			{tag["key"]: tag["value"] for tag in created["tags"]},
			{
				"purpose": "base",
				"os": "Ubuntu",
				"os_version": "24.04",
				"disk_encryption": "luks2",
			},
		)

	def test_different_images_use_different_object_keys_for_the_same_files(self) -> None:
		client = Mock()
		settings = SimpleNamespace(get_object_storage_client=lambda: client)
		with (
			patch("atlas.vm.core.image_builder.frappe.get_single", return_value=settings),
			patch("atlas.vm.core.image_builder.upload_with_progress"),
		):
			first = upload_to_object_storage("image-1", Path("rootfs.img"), Path("kernel"))
			second = upload_to_object_storage("image-2", Path("rootfs.img"), Path("kernel"))

		self.assertEqual(
			first,
			{"image_object_key": "images/image-1/rootfs.img", "kernel_object_key": "images/image-1/kernel"},
		)
		self.assertEqual(
			second,
			{"image_object_key": "images/image-2/rootfs.img", "kernel_object_key": "images/image-2/kernel"},
		)

	def test_object_storage_uploads_an_initrd_owned_by_the_image(self) -> None:
		client = Mock()
		settings = SimpleNamespace(get_object_storage_client=lambda: client)
		with (
			patch("atlas.vm.core.image_builder.frappe.get_single", return_value=settings),
			patch("atlas.vm.core.image_builder.upload_with_progress") as upload,
		):
			location = upload_to_object_storage(
				"image-1", Path("rootfs.img"), Path("kernel"), Path("initrd")
			)

		self.assertEqual(
			location,
			{
				"image_object_key": "images/image-1/rootfs.img",
				"kernel_object_key": "images/image-1/kernel",
				"initrd_object_key": "images/image-1/initrd",
			},
		)
		self.assertEqual(
			upload.call_args_list,
			[
				call(client, Path("rootfs.img"), "images/image-1/rootfs.img"),
				call(client, Path("kernel"), "images/image-1/kernel"),
				call(client, Path("initrd"), "images/image-1/initrd"),
			],
		)

	def test_site_files_publish_an_initrd_owned_by_the_image(self) -> None:
		with patch(
			"atlas.vm.core.image_builder.publish_public_file_path",
			side_effect=["file-rootfs", "file-kernel", "file-initrd"],
		) as publish_file:
			location = publish_to_site_files(
				"image-1",
				Path("rootfs.img.zst"),
				"a" * 64,
				Path("kernel"),
				"b" * 64,
				Path("initrd"),
				"c" * 64,
			)

		self.assertEqual(
			location,
			{"image_file": "file-rootfs", "kernel_file": "file-kernel", "initrd_file": "file-initrd"},
		)
		self.assertEqual(
			publish_file.call_args_list,
			[
				call(Path("rootfs.img.zst"), "a" * 64, "image-1"),
				call(Path("kernel"), "b" * 64, "image-1"),
				call(Path("initrd"), "c" * 64, "image-1"),
			],
		)

	def test_publish_to_site_files_does_not_touch_object_storage(self) -> None:
		created = {}

		with (
			patch("atlas.vm.core.image_builder.get_available_ubuntu_images", return_value=[]),
			patch("atlas.vm.core.image_builder.frappe.generate_hash", return_value="site-image"),
			patch("atlas.vm.core.image_builder.frappe.get_single") as get_single,
			patch(
				"atlas.vm.core.image_builder.publish_public_file_path",
				side_effect=["file-rootfs", "file-kernel"],
			) as publish_file,
			patch("atlas.vm.core.image_builder.Path.stat", return_value=SimpleNamespace(st_size=MEBIBYTE)),
			patch(
				"atlas.vm.core.image_builder.frappe.get_doc",
				side_effect=lambda values: SimpleNamespace(
					insert=lambda **kwargs: created.update(values | kwargs)
				),
			),
		):
			publish_ubuntu_image_variant(
				title="Ubuntu 24.04",
				version="24.04",
				architecture="amd64",
				image_path=Path("rootfs.ext4"),
				image_sha256="a" * 64,
				kernel_path=Path("kernel"),
				kernel_sha256="b" * 64,
				storage="Site File",
			)

		get_single.assert_not_called()
		self.assertEqual(created["status"], "Available")
		self.assertEqual(created["transfer_progress"], 100)
		self.assertEqual(created["artifact_storage"], "Site File")
		self.assertEqual(created["set_name"], "site-image")
		self.assertEqual(created["image_file"], "file-rootfs")
		self.assertEqual(created["kernel_file"], "file-kernel")
		self.assertEqual(
			publish_file.call_args_list,
			[
				call(Path("rootfs.ext4.zst"), "a" * 64, "site-image"),
				call(Path("kernel"), "b" * 64, "site-image"),
			],
		)
		self.assertNotIn("image_object_key", created)
		self.assertNotIn("kernel_object_key", created)
		self.assertEqual(
			{tag["key"]: tag["value"] for tag in created["tags"]},
			{"purpose": "base", "os": "Ubuntu", "os_version": "24.04"},
		)


class TestUbuntuImageBuilderIntegration(IntegrationTestCase):
	def publish(self, title: str, image_sha256: str, initrd_sha256: str = "c" * 64) -> None:
		with (
			patch(
				"atlas.vm.core.image_builder.get_sha256",
				side_effect=[image_sha256, "b" * 64, initrd_sha256],
			),
			patch("atlas.vm.core.image_builder.Path.stat", return_value=SimpleNamespace(st_size=MEBIBYTE)),
			patch(
				"atlas.vm.core.image_builder.upload_to_object_storage",
				side_effect=lambda name, _image, _kernel, initrd=None: {
					"image_object_key": f"images/{name}/rootfs.img",
					"kernel_object_key": f"images/{name}/kernel",
					**({"initrd_object_key": f"images/{name}/initrd"} if initrd else {}),
				},
			),
		):
			publish_ubuntu_image(
				title,
				"24.04",
				"amd64",
				Path("rootfs.img"),
				Path("kernel"),
				Path("initrd"),
			)

	def test_each_lineage_retires_the_protected_build_before_it(self) -> None:
		title = f"test-ubuntu-{frappe.generate_hash(length=8)}"
		self.publish(title, "a" * 64)
		self.publish(title, "c" * 64)
		self.publish(title, "d" * 64)

		for variant_title in (title, f"{title} (Disk Encryption)"):
			images = frappe.get_all(
				"Virtual Machine Image",
				filters={"title": variant_title},
				fields=["image_sha256", "status", "is_termination_protected"],
			)
			statuses = {
				image.image_sha256[0]: (image.status, image.is_termination_protected) for image in images
			}
			self.assertEqual(
				statuses,
				{"a": ("Archived", 0), "c": ("Archived", 0), "d": ("Available", 1)},
			)

	def test_published_variants_own_their_object_keys(self) -> None:
		title = f"test-ubuntu-{frappe.generate_hash(length=8)}"
		self.publish(title, "a" * 64)

		plain_name = frappe.get_all("Virtual Machine Image", filters={"title": title}, pluck="name")[0]
		plain_image = frappe.get_doc("Virtual Machine Image", plain_name)
		self.assertEqual(plain_image.image_object_key, f"images/{plain_name}/rootfs.img")
		self.assertEqual(plain_image.kernel_object_key, f"images/{plain_name}/kernel")
		self.assertFalse(plain_image.has_initrd)

		encrypted_title = f"{title} (Disk Encryption)"
		encrypted_name = frappe.get_all(
			"Virtual Machine Image", filters={"title": encrypted_title}, pluck="name"
		)[0]
		encrypted_image = frappe.get_doc("Virtual Machine Image", encrypted_name)
		self.assertEqual(encrypted_image.image_object_key, f"images/{encrypted_name}/rootfs.img")
		self.assertEqual(encrypted_image.kernel_object_key, f"images/{encrypted_name}/kernel")
		self.assertEqual(encrypted_image.initrd_object_key, f"images/{encrypted_name}/initrd")
		self.assertTrue(encrypted_image.has_initrd)
		self.assertEqual(
			{tag.key: tag.value for tag in encrypted_image.tags}["disk_encryption"],
			"luks2",
		)

	def test_an_initrd_change_replaces_only_the_encryption_lineage(self) -> None:
		title = f"test-ubuntu-{frappe.generate_hash(length=8)}"
		self.publish(title, "a" * 64, "c" * 64)
		self.publish(title, "a" * 64, "d" * 64)

		plain_images = frappe.get_all(
			"Virtual Machine Image", filters={"title": title}, fields=["status", "is_termination_protected"]
		)
		self.assertEqual(
			[(image.status, image.is_termination_protected) for image in plain_images],
			[("Available", 1)],
		)

		encrypted_images = frappe.get_all(
			"Virtual Machine Image",
			filters={"title": f"{title} (Disk Encryption)"},
			fields=["initrd_sha256", "status", "is_termination_protected"],
		)
		statuses = {
			image.initrd_sha256[0]: (image.status, image.is_termination_protected)
			for image in encrypted_images
		}
		self.assertEqual(statuses, {"c": ("Archived", 0), "d": ("Available", 1)})
