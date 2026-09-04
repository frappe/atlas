from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import click
import frappe
from frappe.commands import pass_context
from frappe.exceptions import SiteNotSpecifiedError
from frappe.utils.bench_helper import CliCtxObj

from atlas.vm.core.virtual_machine_image_manager import bytes_to_mib

if TYPE_CHECKING:
	from atlas.atlas.s3 import S3Client


@click.command("build-ubuntu-base-image")
@click.option("--version", type=click.Choice(["22.04", "24.04"]), required=True)
@click.option("--architecture", type=click.Choice(["amd64"]), default="amd64", show_default=True)
@click.option("--minimal", is_flag=True, help="Build the Ubuntu minimal cloud image.")
@click.option("--title")
@click.option(
	"--output-directory", type=click.Path(path_type=Path), default=Path("./dist"), show_default=True
)
@pass_context
def build_ubuntu_base_image(
	context: CliCtxObj,
	version: str,
	architecture: str,
	minimal: bool,
	title: str | None,
	output_directory: Path,
) -> None:
	"""Build and publish a public Ubuntu server cloud image."""
	if not context.sites:
		raise SiteNotSpecifiedError
	if minimal and version != "24.04":
		raise click.UsageError("minimal images are available only for Ubuntu 24.04")

	image_type = "minimal " if minimal else ""
	click.echo(f"Building Ubuntu {version} {image_type}image for {architecture}")
	image_path, kernel_path = build_ubuntu_image(version, architecture, minimal, output_directory)
	for site in context.sites:
		try:
			frappe.init(site)
			frappe.connect()
			click.echo(f"Publishing files to {site}")
			publish_ubuntu_image(
				title or f"Ubuntu {version}" + (" minimal" if minimal else ""),
				version,
				architecture,
				image_path,
				kernel_path,
			)
			frappe.db.commit()
			click.echo(f"Created Virtual Machine Image for {site}")
		finally:
			frappe.destroy()


def build_ubuntu_image(
	version: str, platform: str, minimal: bool, output_directory: Path
) -> tuple[Path, Path]:
	output_directory.mkdir(parents=True, exist_ok=True)
	image_type = "minimal-" if minimal else ""
	image_path = output_directory / f"ubuntu-{version}-{image_type}{platform}.ext4"
	kernel_path = output_directory / f"vmlinux-ubuntu-{version}-{image_type}server"
	builder_path = Path(__file__).parent / "vm" / "scripts" / "build_ubuntu_server_image.sh"
	command = [builder_path]
	if os.geteuid() != 0:
		command.insert(0, "sudo")
	command.extend(
		[
			"--output",
			image_path,
			"--kernel-output",
			kernel_path,
			"--platform",
			platform,
			"--version",
			version,
			"--minimal" if minimal else "",
		]
	)
	command = [argument for argument in command if argument]
	subprocess.run(command, check=True)
	return image_path, kernel_path


def publish_ubuntu_image(
	title: str, version: str, platform: str, image_path: Path, kernel_path: Path
) -> None:
	from atlas.atlas.s3 import S3Error

	image_sha256 = _sha256(image_path)
	kernel_sha256 = _sha256(kernel_path)
	existing_name = frappe.db.exists("Virtual Machine Image", {"title": title})
	if existing_name:
		existing = frappe.get_doc("Virtual Machine Image", existing_name)
		if existing.image_sha256 == image_sha256 and existing.kernel_sha256 == kernel_sha256:
			return

	image_key = f"vm-images/sha256/{image_sha256}/{image_path.name}"
	kernel_key = f"vm-images/sha256/{kernel_sha256}/{kernel_path.name}"

	settings = frappe.get_single("Atlas Settings")
	try:
		s3_client = settings.get_s3_client()
		_upload_with_progress(s3_client, image_path, image_key)
		_upload_with_progress(s3_client, kernel_path, kernel_key)
	except S3Error as error:
		raise click.UsageError(str(error)) from error

	file_values = {
		"status": "Available",
		"image_object_key": image_key,
		"image_sha256": image_sha256,
		"image_size_mib": bytes_to_mib(image_path.stat().st_size),
		"kernel_object_key": kernel_key,
		"kernel_sha256": kernel_sha256,
		"kernel_size_mib": bytes_to_mib(kernel_path.stat().st_size),
	}

	if existing_name:
		existing.update(file_values)
		existing.version = (existing.version or 1) + 1
		existing.save()
		return

	frappe.get_doc(
		{
			"doctype": "Virtual Machine Image",
			"title": title,
			"version": 1,
			"image_type": "System",
			"platform": platform,
			"operating_system": "Ubuntu",
			"operating_system_version": version,
			"supports_cloud_init": 1,
			**file_values,
		}
	).insert()


def _sha256(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open("rb") as source:
		for chunk_data in iter(lambda: source.read(1024 * 1024), b""):
			digest.update(chunk_data)
	return digest.hexdigest()


def _upload_with_progress(s3_client: "S3Client", source: Path, key: str) -> None:
	"""Upload a file to S3 and show its progress."""
	total_bytes = source.stat().st_size
	transferred_bytes = 0
	progress_lock = threading.Lock()

	def on_progress(chunk_bytes: int) -> None:
		nonlocal transferred_bytes
		with progress_lock:
			transferred_bytes += chunk_bytes
			percentage = transferred_bytes / total_bytes * 100 if total_bytes else 100
			click.echo(
				f"\rUploading {source.name}: {transferred_bytes >> 20}/{total_bytes >> 20} MiB ({percentage:5.1f}%)",
				nl=False,
			)

	s3_client.upload_file(str(source), key, on_progress=on_progress)
	click.echo()


commands = [build_ubuntu_base_image]
