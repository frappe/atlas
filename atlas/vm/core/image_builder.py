from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

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


def build_ubuntu_image(
	version: str, architecture: str, minimal: bool, output_directory: Path
) -> tuple[Path, Path]:
	"""Build one Ubuntu root file system and kernel."""
	output_directory = output_directory.resolve()
	output_directory.mkdir(parents=True, exist_ok=True)
	image_type = "minimal-" if minimal else ""
	image_path = output_directory / f"ubuntu-{version}-{image_type}{architecture}.ext4"
	kernel_path = output_directory / f"vmlinux-ubuntu-{version}-{image_type}server"
	builder_path = Path(__file__).parents[1] / "scripts" / "build_ubuntu_server_image.sh"
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
	command.extend(
		[
			"--output",
			image_path,
			"--kernel-output",
			kernel_path,
			"--architecture",
			architecture,
			"--version",
			version,
			"--minimal" if minimal else "",
		]
	)
	subprocess.run([argument for argument in command if argument], check=True)
	return image_path, kernel_path


def publish_ubuntu_image(
	title: str,
	version: str,
	architecture: str,
	image_path: Path,
	kernel_path: Path,
	storage: ArtifactStorage = "Object Storage",
) -> None:
	"""Publish Ubuntu artifacts as a new image record and retire the records it replaces.

	A record never changes its artifacts, because a VM keeps using the image it started from.
	"""
	image_sha256 = get_sha256(image_path)
	kernel_sha256 = get_sha256(kernel_path)
	previous_images = get_available_ubuntu_images(title, architecture)
	for image in previous_images:
		if image.image_sha256 == image_sha256 and image.kernel_sha256 == kernel_sha256:
			return

	# The digest and size describe the raw file system. Hosts download the zstd copy and verify the decoded bytes.
	compressed_image_path = image_path.with_name(f"{image_path.name}.zst")
	image_name = frappe.generate_hash(length=16)
	if storage == "Site File":
		location = publish_to_site_files(
			image_name, compressed_image_path, image_sha256, kernel_path, kernel_sha256
		)
	else:
		location = upload_to_object_storage(image_name, compressed_image_path, kernel_path)

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
			"kernel_sha256": kernel_sha256,
			"kernel_size_mib": bytes_to_mib(kernel_path.stat().st_size),
			"tags": [
				{"key": "purpose", "value": "base"},
				{"key": "os", "value": "Ubuntu"},
				{"key": "os_version", "value": version},
			],
			**location,
		}
	).insert(set_name=image_name)

	# Every System image is protected at insert. The builder owns the build it replaces.
	deletion = VirtualMachineImageDeletionService()
	for image in previous_images:
		image.is_termination_protected = 0
		deletion.request(image)


def get_available_ubuntu_images(title: str, architecture: str) -> list[VirtualMachineImage]:
	"""Return the Available System images that a new build with this title replaces."""
	names = frappe.get_all(
		"Virtual Machine Image",
		filters={"title": title, "architecture": architecture, "image_type": "system", "status": "Available"},
		pluck="name",
	)
	return [cast("VirtualMachineImage", frappe.get_doc("Virtual Machine Image", name)) for name in names]


def upload_to_object_storage(image_name: str, image_path: Path, kernel_path: Path) -> dict[str, str]:
	"""Upload both artifacts under keys owned by this image."""
	image_key = f"images/{image_name}/rootfs.img"
	kernel_key = f"images/{image_name}/kernel"
	settings = frappe.get_single("Atlas Settings")
	object_storage_client = settings.get_object_storage_client()
	upload_with_progress(object_storage_client, image_path, image_key)
	upload_with_progress(object_storage_client, kernel_path, kernel_key)
	return {"image_object_key": image_key, "kernel_object_key": kernel_key}


def publish_to_site_files(
	image_name: str, image_path: Path, image_sha256: str, kernel_path: Path, kernel_sha256: str
) -> dict[str, str]:
	"""Attach both artifacts as public site files for a host to download."""
	click.echo(f"Publishing {image_path.name} and {kernel_path.name} as public site files")
	return {
		"image_file": publish_public_file_path(image_path, image_sha256, image_name),
		"kernel_file": publish_public_file_path(kernel_path, kernel_sha256, image_name),
	}


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
