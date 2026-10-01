from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

import frappe
from frappe import _
from frappe.utils import now_datetime

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.core.exceptions import AtlasUserError
from atlas.atlas.object_storage import ObjectStorageError

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.atlas.object_storage import ObjectStorageClient
	from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import (
		Artifact,
		VirtualMachineImage,
	)

MIGRATION_TIMEOUT_SECONDS = 3600
SITE_FILE_RETENTION = timedelta(hours=6)


class VirtualMachineImageStorageMigration:
	"""Move one bootstrap System image into object storage."""

	def request(self, image: VirtualMachineImage) -> None:
		"""Queue one repeatable migration for a site file image."""
		if not image.is_stored_in_site_file:
			frappe.throw(_("This image is already in object storage."), exc=AtlasUserError)
		if image.status != "Available":
			frappe.throw(_("Only an Available image can be migrated."), exc=AtlasUserError)

		self.enqueue(cast(str, image.name))

	def enqueue(self, image_name: str) -> None:
		"""Enqueue one repeatable storage migration."""
		frappe.enqueue(
			"atlas.vm.core.vm_image_storage_migration.migrate_virtual_machine_image_storage",
			queue="long",
			timeout=MIGRATION_TIMEOUT_SECONDS,
			image_name=image_name,
			job_id=f"atlas||system-image||storage-migration||{image_name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	def migrate(self, image_name: str) -> None:
		"""Upload the image artifacts and move the record to object storage.

		The site files stay for SITE_FILE_RETENTION, so a host that is still
		downloading one keeps a working URL. delete_expired_site_files removes them.
		"""
		image = cast("VirtualMachineImage", frappe.get_doc("Virtual Machine Image", image_name))
		if not image.is_stored_in_site_file:
			return

		client = cast("AtlasSettings", frappe.get_single("Atlas Settings")).get_object_storage_client()
		image.image_object_key = self.upload(client, image, "rootfs")
		image.kernel_object_key = self.upload(client, image, "kernel")
		if getattr(image, "has_initrd", False):
			image.initrd_object_key = self.upload(client, image, "initrd")
		image.artifact_storage = "Object Storage"
		image.site_file_retention_until = now_datetime() + SITE_FILE_RETENTION
		image.save()

		from atlas.service.doctype.cargo_server.cargo_server import enqueue_pilot_release_tracker_enable

		enqueue_pilot_release_tracker_enable()

	def delete_site_files(self, image_name: str) -> None:
		"""Remove the site files that object storage replaced.

		The files go before the record drops the retention time, so a failed delete
		leaves the image for the next sweep instead of an unreachable file. The
		artifacts already serve from object storage, so a stale reference reaches
		nothing, and deleting a file that is gone does nothing.
		"""
		image = cast("VirtualMachineImage", frappe.get_doc("Virtual Machine Image", image_name))
		if image.is_stored_in_site_file:
			return

		for file_name in (image.image_file, image.kernel_file, getattr(image, "initrd_file", None)):
			if file_name:
				frappe.delete_doc(
					"File", file_name, force=True, ignore_permissions=True, delete_permanently=True
				)

		image.image_file = None
		image.kernel_file = None
		image.initrd_file = None
		image.site_file_retention_until = None
		image.save()

	def upload(self, client: ObjectStorageClient, image: VirtualMachineImage, artifact: Artifact) -> str:
		"""Upload one artifact under this image's key and verify its size."""
		file_name, key = {
			"rootfs": (image.image_file, f"images/{image.name}/rootfs.img"),
			"kernel": (image.kernel_file, f"images/{image.name}/kernel"),
			"initrd": (getattr(image, "initrd_file", None), f"images/{image.name}/initrd"),
		}[artifact]
		if not file_name:
			frappe.throw(_("Virtual Machine Image {0} has no {1} file.").format(image.name, artifact))

		source = Path(frappe.get_doc("File", file_name).get_full_path())
		client.upload_file(str(source), key)
		self.validate_stored_size(client, key, source.stat().st_size)
		return key

	@staticmethod
	def validate_stored_size(client: ObjectStorageClient, key: str, expected_bytes: int) -> None:
		"""Reject a stored object whose size does not match the local file."""
		metadata = client.head_object(key)
		stored_bytes = metadata.get("ContentLength") if metadata else None
		if stored_bytes != expected_bytes:
			raise ObjectStorageError(
				f"Stored object {key} is {stored_bytes} bytes and the local file is {expected_bytes} bytes"
			)


def enqueue_site_file_image_migrations() -> None:
	"""Queue migrations for Available images stored as site files."""
	settings = cast("AtlasSettings", frappe.get_single("Atlas Settings"))
	if not settings.is_object_storage_configured:
		return

	migration = VirtualMachineImageStorageMigration()
	for name in frappe.get_all(
		"Virtual Machine Image",
		filters={"artifact_storage": "Site File", "status": "Available"},
		pluck="name",
	):
		migration.enqueue(name)


@run_as_admin
def delete_expired_site_files() -> None:
	"""Remove the site files of every image that finished its retention time."""
	migration = VirtualMachineImageStorageMigration()
	for name in frappe.get_all(
		"Virtual Machine Image",
		filters=[
			["artifact_storage", "=", "Object Storage"],
			["site_file_retention_until", "is", "set"],
			["site_file_retention_until", "<=", now_datetime()],
		],
		pluck="name",
	):
		migration.delete_site_files(name)


@run_as_admin
def migrate_virtual_machine_image_storage(image_name: str) -> None:
	"""Run one queued storage migration and record a visible failure."""
	try:
		VirtualMachineImageStorageMigration().migrate(image_name)
	except ObjectStorageError as error:
		frappe.db.set_value("Virtual Machine Image", image_name, "transfer_error", str(error)[:1000])
		frappe.log_error(
			title=f"System image storage migration failed for {image_name}",
			message=frappe.get_traceback(),
		)
