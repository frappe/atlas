from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import click
import frappe

from atlas.atlas.core.artifacts import publish_public_file_path
from atlas.atlas.core.host_binaries import BUILDER_IMAGE, IS_MACOS
from atlas.vm.core.multipart_upload import bytes_to_mib
from atlas.vm.core.vm_image_deletion import VirtualMachineImageDeletionService

if TYPE_CHECKING:
	from atlas.atlas.object_storage import ObjectStorageClient
	from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage

ArtifactStorage = Literal["Object Storage", "Site File"]
ImagePurpose = Literal["base", "rescue"]


def build_ubuntu_image(
	version: str, architecture: str, minimal: bool, output_directory: Path
) -> tuple[Path, Path]:
	"""Build one Ubuntu base root file system and kernel."""
	image_type = "minimal-" if minimal else ""
	return _build_image(
		"build_ubuntu_server_image.sh",
		output_directory,
		f"ubuntu-{version}-{image_type}{architecture}.ext4",
		f"vmlinux-ubuntu-{version}-{image_type}server",
		["--architecture", architecture, "--version", version, *(["--minimal"] if minimal else [])],
	)


def build_ubuntu_rescue_image(output_directory: Path) -> tuple[Path, Path]:
	"""Build the Ubuntu 24.04 amd64 rescue root file system and matching kernel."""
	return _build_image(
		"build_ubuntu_rescue_image.sh",
		output_directory,
		"ubuntu-24.04-rescue-amd64.ext4",
		"vmlinux-ubuntu-24.04-rescue-server",
		[],
	)


def _build_image(
	script: str, output_directory: Path, image_name: str, kernel_name: str, arguments: list[str]
) -> tuple[Path, Path]:
	output_directory = output_directory.resolve()
	output_directory.mkdir(parents=True, exist_ok=True)
	image_path = output_directory / image_name
	kernel_path = output_directory / kernel_name
	builder_path = Path(__file__).parents[1] / "scripts" / script
	command = [builder_path]
	if IS_MACOS:
		command = [
			"docker",
			"run",
			"--rm",
			f"--volume={builder_path.parent}:{builder_path.parent}",
			f"--volume={output_directory}:{output_directory}",
			BUILDER_IMAGE,
			builder_path,
		]
	elif os.geteuid() != 0:
		command.insert(0, "sudo")
	command.extend(["--output", image_path, "--kernel-output", kernel_path, *arguments])
	subprocess.run(command, check=True)
	return image_path, kernel_path


def publish_ubuntu_image(
	title: str,
	version: str,
	architecture: str,
	image_path: Path,
	kernel_path: Path,
	storage: ArtifactStorage = "Object Storage",
	minimal: bool = False,
	*,
	purpose: ImagePurpose = "base",
) -> None:
	"""Publish Ubuntu artifacts, retiring previous builds only for base images.

	A record never changes its artifacts, because a VM keeps using the image it started from.
	"""
	image_sha256 = get_sha256(image_path)
	kernel_sha256 = get_sha256(kernel_path)
	previous_images = get_available_ubuntu_images(title, architecture, purpose)
	for image in previous_images:
		if image.image_sha256 == image_sha256 and image.kernel == kernel_sha256:
			return

	kernel_release = kernel_path.with_name(f"{kernel_path.name}.release").read_text().strip()
	store_kernel(
		f"ubuntu-{version} - kernel {kernel_release}", kernel_path, kernel_sha256, architecture, storage
	)
	# The digest and size describe the raw file system. Hosts download the zstd copy and verify the decoded bytes.
	compressed_image_path = image_path.with_name(f"{image_path.name}.zst")
	image_name = frappe.generate_hash(length=16)
	if storage == "Site File":
		location = {"image_file": publish_public_file_path(compressed_image_path, image_sha256, image_name)}
	else:
		location = {"image_object_key": f"images/{image_name}/rootfs.img"}
		upload_with_progress(
			frappe.get_single("Atlas Settings").get_object_storage_client(),
			compressed_image_path,
			location["image_object_key"],
		)

	frappe.get_doc(
		{
			"doctype": "Virtual Machine Image",
			"title": title,
			"image_type": "system",
			"architecture": architecture,
			"status": "Available",
			"transfer_progress": 100,
			"artifact_storage": storage,
			"image_sha256": image_sha256,
			"image_size_mib": bytes_to_mib(image_path.stat().st_size),
			"image_stored_size_mib": bytes_to_mib(compressed_image_path.stat().st_size),
			"kernel": kernel_sha256,
			"cache_image": int(purpose == "rescue"),
			"tags": [
				{"key": "purpose", "value": purpose},
				{"key": "os", "value": "Ubuntu"},
				{"key": "os_version", "value": version},
				{"key": "minimal", "value": "true" if minimal else "false"},
			],
			**location,
		}
	).insert(set_name=image_name)

	if purpose == "rescue":
		frappe.db.set_single_value("Atlas Settings", "rescue_virtual_machine_image", image_name)
		return

	# Every System image is protected at insert. The builder owns the build it replaces.
	deletion = VirtualMachineImageDeletionService()
	for image in previous_images:
		image.is_termination_protected = 0
		deletion.request(image)


def get_available_ubuntu_images(
	title: str, architecture: str, purpose: ImagePurpose = "base"
) -> list[VirtualMachineImage]:
	"""Return Available System images with the same title, architecture, and purpose."""
	names = frappe.get_all(
		"Virtual Machine Image",
		filters={"title": title, "architecture": architecture, "image_type": "system", "status": "Available"},
		pluck="name",
	)
	images = [frappe.get_doc("Virtual Machine Image", name) for name in names]
	# An image without a purpose tag predates the tag and is a base image.
	return [
		image
		for image in images
		if {tag.key: tag.value for tag in image.tags}.get("purpose", "base") == purpose
	]


def store_kernel(
	title: str, kernel_path: Path, kernel_sha256: str, architecture: str, storage: ArtifactStorage
) -> None:
	"""Store a kernel once. A later build with the same kernel reuses it."""
	if frappe.db.exists("Virtual Machine Kernel", kernel_sha256):
		return

	if storage == "Site File":
		location = {"file": publish_public_file_path(kernel_path, kernel_sha256, "kernel")}
	else:
		location = {"object_key": f"kernels/{kernel_sha256}"}
		upload_with_progress(
			frappe.get_single("Atlas Settings").get_object_storage_client(),
			kernel_path,
			location["object_key"],
		)
	frappe.get_doc(
		{
			"doctype": "Virtual Machine Kernel",
			"title": title,
			"sha256": kernel_sha256,
			"architecture": architecture,
			"size_mib": bytes_to_mib(kernel_path.stat().st_size),
			**location,
		}
	).insert()


def get_sha256(path: Path) -> str:
	"""Return the SHA-256 value for one file."""
	digest = hashlib.sha256()
	with path.open("rb") as source:
		for chunk_data in iter(lambda: source.read(1024 * 1024), b""):
			digest.update(chunk_data)
	return digest.hexdigest()


def upload_with_progress(object_storage_client: ObjectStorageClient, source: Path, key: str) -> None:
	"""Upload a file to object storage and show its progress."""
	total_bytes = source.stat().st_size
	transferred_bytes = 0
	progress_lock = threading.Lock()

	def on_progress(chunk_bytes: int) -> None:
		"""Record build progress on the image record."""
		nonlocal transferred_bytes
		with progress_lock:
			transferred_bytes += chunk_bytes
			percentage = transferred_bytes / total_bytes * 100 if total_bytes else 100
			click.echo(
				f"\rUploading {source.name}: {transferred_bytes >> 20}/{total_bytes >> 20} MiB ({percentage:5.1f}%)",
				nl=False,
			)

	object_storage_client.upload_file(str(source), key, on_progress=on_progress)
	click.echo()
