from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from atlas.vm.core.image_builder import (
	build_ubuntu_image,
	get_available_ubuntu_images,
	publish_ubuntu_image,
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
			image_path, kernel_path = build_ubuntu_image("24.04", "amd64", True, Path(temporary_directory))

		command = run.call_args.args[0]
		self.assertEqual(command[0], "sudo")
		self.assertTrue(str(command[1]).endswith("vm/scripts/build_ubuntu_server_image.sh"))
		self.assertIn("--minimal", command)
		self.assertEqual(image_path.name, "ubuntu-24.04-minimal-amd64.ext4")
		self.assertEqual(kernel_path.name, "vmlinux-ubuntu-24.04-minimal-server")
		run.assert_called_once_with(command, check=True)

	def test_publish_keeps_an_unchanged_image_record(self) -> None:
		existing = SimpleNamespace(image_sha256="a" * 64, kernel="b" * 64)
		with (
			patch("atlas.vm.core.image_builder.get_sha256", side_effect=["a" * 64, "b" * 64]) as sha256,
			patch("atlas.vm.core.image_builder.get_available_ubuntu_images", return_value=[existing]),
			patch("atlas.vm.core.image_builder.frappe.get_single") as get_single,
			patch("atlas.vm.core.image_builder.VirtualMachineImageDeletionService") as deletion,
		):
			publish_ubuntu_image(
				"Ubuntu 24.04",
				"24.04",
				"amd64",
				Path("rootfs.img"),
				Path("kernel"),
			)

		self.assertEqual(sha256.call_args_list, [call(Path("rootfs.img")), call(Path("kernel"))])
		get_single.assert_not_called()
		deletion.assert_not_called()

	def test_publish_adds_a_new_record_and_retires_the_previous_one(self) -> None:
		previous = SimpleNamespace(image_sha256="c" * 64, kernel="b" * 64, is_termination_protected=1)
		created = {}

		with (
			patch("atlas.vm.core.image_builder.get_sha256", side_effect=["a" * 64, "b" * 64]),
			patch("atlas.vm.core.image_builder.get_available_ubuntu_images", return_value=[previous]),
			patch("atlas.vm.core.image_builder.frappe.generate_hash", return_value="new-image"),
			patch("atlas.vm.core.image_builder.frappe.get_single"),
			patch("atlas.vm.core.image_builder.store_kernel") as store_kernel,
			patch("atlas.vm.core.image_builder.upload_with_progress") as upload,
			patch("atlas.vm.core.image_builder.Path.stat", return_value=SimpleNamespace(st_size=MEBIBYTE)),
			patch("atlas.vm.core.image_builder.Path.read_text", return_value="6.17.0-5-generic\n"),
			patch(
				"atlas.vm.core.image_builder.frappe.get_doc",
				side_effect=lambda values: SimpleNamespace(
					insert=lambda **kwargs: created.update(values | kwargs)
				),
			),
			patch("atlas.vm.core.image_builder.VirtualMachineImageDeletionService") as deletion,
		):
			publish_ubuntu_image("ubuntu-24.04-minimal", "24.04", "amd64", Path("rootfs.img"), Path("kernel"))

		self.assertEqual(created["set_name"], "new-image")
		self.assertNotIn("version", created)
		self.assertEqual(created["image_object_key"], "images/new-image/rootfs.img")
		self.assertEqual(created["kernel"], "b" * 64)
		store_kernel.assert_called_once_with(
			"ubuntu-24.04 - kernel 6.17.0-5-generic", Path("kernel"), "b" * 64, "amd64", "Object Storage"
		)
		self.assertEqual(upload.call_args.args[1:], (Path("rootfs.img.zst"), "images/new-image/rootfs.img"))
		self.assertEqual(created["image_stored_size_mib"], 1)
		deletion.return_value.request.assert_called_once_with(previous)
		self.assertEqual(previous.is_termination_protected, 0)

	def test_publish_to_site_files_does_not_touch_object_storage(self) -> None:
		created = {}

		with (
			patch("atlas.vm.core.image_builder.get_sha256", side_effect=["a" * 64, "b" * 64]),
			patch("atlas.vm.core.image_builder.get_available_ubuntu_images", return_value=[]),
			patch("atlas.vm.core.image_builder.frappe.generate_hash", return_value="site-image"),
			patch("atlas.vm.core.image_builder.frappe.get_single") as get_single,
			patch("atlas.vm.core.image_builder.frappe.db.exists", return_value=False),
			patch(
				"atlas.vm.core.image_builder.publish_public_file_path",
				side_effect=["file-kernel", "file-rootfs"],
			) as publish_file,
			patch("atlas.vm.core.image_builder.Path.stat", return_value=SimpleNamespace(st_size=MEBIBYTE)),
			patch("atlas.vm.core.image_builder.Path.read_text", return_value="6.17.0-5-generic\n"),
			patch(
				"atlas.vm.core.image_builder.frappe.get_doc",
				side_effect=lambda values: SimpleNamespace(
					insert=lambda **kwargs: created.update(values | kwargs)
				),
			),
		):
			publish_ubuntu_image(
				"Ubuntu 24.04",
				"24.04",
				"amd64",
				Path("rootfs.ext4"),
				Path("kernel"),
				"Site File",
			)

		get_single.assert_not_called()
		self.assertEqual(created["status"], "Available")
		self.assertEqual(created["transfer_progress"], 100)
		self.assertEqual(created["artifact_storage"], "Site File")
		self.assertEqual(created["set_name"], "site-image")
		self.assertEqual(created["image_file"], "file-rootfs")
		self.assertEqual(created["kernel"], "b" * 64)
		self.assertEqual(
			publish_file.call_args_list,
			[call(Path("kernel"), "b" * 64, "kernel"), call(Path("rootfs.ext4.zst"), "a" * 64, "site-image")],
		)
		self.assertNotIn("image_object_key", created)
		self.assertEqual(
			{tag["key"]: tag["value"] for tag in created["tags"]},
			{"purpose": "base", "os": "Ubuntu", "os_version": "24.04", "minimal": "false"},
		)


class TestUbuntuImageBuilderIntegration(IntegrationTestCase):
	def publish(self, title: str, image_sha256: str, purpose="base") -> Mock:
		with (
			patch("atlas.vm.core.image_builder.get_sha256", side_effect=[image_sha256, "b" * 64]),
			patch("atlas.vm.core.image_builder.Path.stat", return_value=SimpleNamespace(st_size=MEBIBYTE)),
			patch("atlas.vm.core.image_builder.Path.read_text", return_value="6.17.0-5-generic\n"),
			patch("atlas.vm.core.image_builder.frappe.get_single"),
			patch("atlas.vm.core.image_builder.upload_with_progress") as upload,
		):
			publish_ubuntu_image(title, "24.04", "amd64", Path("rootfs.img"), Path("kernel"), purpose=purpose)
		return upload

	def test_each_build_retires_the_protected_build_before_it(self) -> None:
		title = f"test-ubuntu-{frappe.generate_hash(length=8)}"
		self.publish(title, "a" * 64)
		self.publish(title, "c" * 64)
		self.publish(title, "d" * 64)

		images = frappe.get_all(
			"Virtual Machine Image",
			filters={"title": title},
			fields=["image_sha256", "status", "is_termination_protected"],
		)
		statuses = {image.image_sha256[0]: (image.status, image.is_termination_protected) for image in images}
		self.assertEqual(statuses, {"a": ("Archived", 0), "c": ("Archived", 0), "d": ("Available", 1)})

	def test_builds_with_one_kernel_upload_it_once(self) -> None:
		title = f"test-ubuntu-{frappe.generate_hash(length=8)}"
		first_upload = self.publish(title, "a" * 64)
		second_upload = self.publish(title, "c" * 64)

		uploaded_keys = [
			upload.args[2] for upload in first_upload.call_args_list + second_upload.call_args_list
		]
		self.assertEqual(
			[key for key in uploaded_keys if key.startswith("kernels/")], [f"kernels/{'b' * 64}"]
		)
		for name in frappe.get_all("Virtual Machine Image", filters={"title": title}, pluck="name"):
			image = frappe.get_doc("Virtual Machine Image", name)
			self.assertEqual(image.image_object_key, f"images/{name}/rootfs.img")
			self.assertEqual(image.kernel, "b" * 64)
			self.assertEqual(image.kernel_size_mib, 1)
			self.assertEqual(image.version, 1)

	def test_rescue_builds_remain_available_and_do_not_replace_a_matching_base_title(self) -> None:
		title = f"test-rescue-{frappe.generate_hash(length=8)}"
		self.publish(title, "a" * 64)
		self.publish(title, "b" * 64, "rescue")
		self.publish(title, "c" * 64, "rescue")
		images = frappe.get_all(
			"Virtual Machine Image",
			filters={"title": title},
			fields=["name", "status", "is_termination_protected"],
		)
		self.assertEqual(len(images), 3)
		for image in images:
			self.assertEqual((image.status, image.is_termination_protected), ("Available", 1))
		self.assertEqual(len(get_available_ubuntu_images(title, "amd64", "rescue")), 2)
		self.assertEqual(len(get_available_ubuntu_images(title, "amd64", "base")), 1)

		selectable = frappe.get_list(
			"Virtual Machine Image",
			filters=[
				["Virtual Machine Image", "title", "=", title],
				["Virtual Machine Image", "enabled", "=", 1],
				["Virtual Machine Image", "image_type", "=", "system"],
				["Virtual Machine Image", "status", "=", "Available"],
				["Virtual Machine Image", "memory_snapshot", "=", 0],
				["Atlas Tag", "key", "=", "purpose"],
				["Atlas Tag", "value", "=", "rescue"],
			],
		)
		self.assertEqual(len(selectable), 2)

		latest = frappe.db.get_value(
			"Virtual Machine Image",
			{"title": title, "image_sha256": "c" * 64},
			["name", "cache_image"],
			as_dict=True,
		)
		self.assertEqual(latest.cache_image, 1)
		self.assertEqual(
			frappe.db.get_single_value("Atlas Settings", "rescue_virtual_machine_image"), latest.name
		)
