from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, cast

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.core.exceptions import AtlasUserError
from atlas.atlas.object_storage import ObjectStorageError
from atlas.vm.core.metal_client import MetalClient, MetalClientError

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer
	from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage


DELETION_TIMEOUT_SECONDS = 900
IMAGE_ARTIFACT_RETENTION = timedelta(hours=6)


class VirtualMachineImageDeletionService:
	"""Own the deletion of one image."""

	def request(self, image: VirtualMachineImage) -> str:
		"""Retire one image and keep its artifacts for six hours."""
		if image.status in ("Deleting", "Archived"):
			return cast(str, image.status)

		image.ensure_not_termination_protected()

		if image.status not in ("Available", "Failed"):
			frappe.throw(_("Only an Available or Failed image can be deleted."), exc=AtlasUserError)

		image.artifact_retention_until = now_datetime() + IMAGE_ARTIFACT_RETENTION
		image.status = "Archived"
		image.enabled = 0
		image.save()
		return cast(str, image.status)

	def reclaim_archived(self) -> None:
		"""Queue the deletion of each archived image that no VM references.

		A VM can start or migrate again at any time and then needs its image artifacts.
		"""
		for image in frappe.get_all(
			"Virtual Machine Image",
			filters={"status": "Archived"},
			fields=[
				"name",
				"artifact_retention_until",
				"site_file_retention_until",
				"image_object_key",
				"rootfs_multipart_upload_id",
				"image_file",
				"source_server",
			],
		):
			if is_retaining_artifacts(image) or has_virtual_machines(image.name):
				continue
			frappe.db.set_value("Virtual Machine Image", image.name, "status", "Deleting")
			self.enqueue(image.name)

	def enqueue(self, image_name: str) -> None:
		"""Enqueue one repeatable image cleanup."""
		frappe.enqueue(
			"atlas.vm.core.vm_image_deletion.delete_virtual_machine_image",
			queue="long",
			timeout=DELETION_TIMEOUT_SECONDS,
			image_name=image_name,
			job_id=f"atlas||image||deletion||{image_name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	def delete(self, image_name: str) -> None:
		"""Remove the artifacts and the record of an image that no VM references."""
		image: VirtualMachineImage = frappe.get_doc("Virtual Machine Image", image_name)
		if is_retaining_artifacts(image) or has_virtual_machines(image_name):
			return
		if image.image_object_key:
			settings: AtlasSettings = frappe.get_single("Atlas Settings")
			client = settings.get_object_storage_client()
			if image.rootfs_multipart_upload_id:
				client.abort_multipart_upload(image.image_object_key, image.rootfs_multipart_upload_id)
			client.delete_object(image.image_object_key)
			# An image stored its own kernel copy before kernels were shared.
			legacy_kernel_key = f"images/{image.name}/kernel"
			if legacy_kernel_key != frappe.db.get_value("Virtual Machine Kernel", image.kernel, "object_key"):
				client.delete_object(legacy_kernel_key)
		self.delete_staged_snapshot(image)
		# The image still links its owned file until cleanup is saved.
		if image.image_file:
			frappe.delete_doc(
				"File", image.image_file, force=True, ignore_permissions=True, delete_permanently=True
			)
		if has_stored_artifacts(image):
			for field in (
				"image_object_key",
				"rootfs_multipart_upload_id",
				"image_file",
				"source_server",
				"site_file_retention_until",
			):
				setattr(image, field, None)
			image.save()
		if image.status != "Deleting":
			frappe.db.set_value("Virtual Machine Image", image_name, "status", "Deleting")
		frappe.delete_doc("Virtual Machine Image", image_name)

	@staticmethod
	def delete_staged_snapshot(image: VirtualMachineImage) -> None:
		"""Remove Metal staging data that a stopped transfer left behind."""
		if not image.source_server or not frappe.db.exists("Metal Server", image.source_server):
			return

		server: MetalServer = frappe.get_doc("Metal Server", image.source_server)
		try:
			MetalClient(server).delete_snapshot(cast(str, image.name))
		except MetalClientError as error:
			if not error.is_not_found:
				raise


def is_retaining_artifacts(image: VirtualMachineImage) -> bool:
	"""Keep downloads available through both retirement and migration deadlines."""
	return any(
		deadline and get_datetime(deadline) > now_datetime()
		for deadline in (image.artifact_retention_until, image.site_file_retention_until)
	)


def has_stored_artifacts(image: VirtualMachineImage) -> bool:
	return any(
		getattr(image, field)
		for field in (
			"image_object_key",
			"rootfs_multipart_upload_id",
			"image_file",
			"source_server",
		)
	)


def has_virtual_machines(image_name: str) -> bool:
	return bool(frappe.db.exists("Virtual Machine", {"virtual_machine_image": image_name}))


def enqueue_pending_virtual_machine_image_deletions() -> None:
	"""Resume unfinished deletions and reclaim an archived image that is now unused."""
	service = VirtualMachineImageDeletionService()
	for name in frappe.get_all(
		"Virtual Machine Image",
		filters={"status": "Deleting"},
		pluck="name",
	):
		service.enqueue(name)

	service.reclaim_archived()


@run_as_admin
def delete_virtual_machine_image(image_name: str) -> None:
	"""Run one queued image cleanup and record a visible failure."""
	try:
		VirtualMachineImageDeletionService().delete(image_name)
	except (MetalClientError, ObjectStorageError) as error:
		frappe.db.set_value("Virtual Machine Image", image_name, "transfer_error", str(error)[:1000])
		frappe.log_error(
			title=f"Image deletion failed for {image_name}",
			message=frappe.get_traceback(),
		)


@run_as_admin
def delete_unused_kernels() -> None:
	"""Remove each kernel that no image uses, and retry a failed one on the next run."""
	used_kernels = set(frappe.get_all("Virtual Machine Image", pluck="kernel", distinct=True))
	for kernel in frappe.get_all("Virtual Machine Kernel", pluck="name"):
		if kernel in used_kernels:
			continue
		try:
			frappe.delete_doc("Virtual Machine Kernel", kernel, ignore_permissions=True)
		except ObjectStorageError:
			frappe.log_error(title=f"Kernel deletion failed for {kernel}", message=frappe.get_traceback())
