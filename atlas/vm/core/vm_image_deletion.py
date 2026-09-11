from __future__ import annotations

from typing import TYPE_CHECKING, cast

import frappe
from frappe import _

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.core.exceptions import AtlasUserError
from atlas.atlas.object_storage import ObjectStorageError
from atlas.vm.core.metal_client import MetalClient, MetalClientError

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.atlas.object_storage import ObjectStorageClient
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer
	from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage


DELETION_TIMEOUT_SECONDS = 900


class VirtualMachineImageInUse(AtlasUserError):
	"""Report that a virtual machine uses the image."""

	http_status_code = 409


class VirtualMachineImageDeletionService:
	"""Own the deletion of one Machine image."""

	def request(self, image: VirtualMachineImage) -> None:
		"""Mark one unused Machine image for deletion and queue its cleanup."""
		if image.image_type != "machine":
			frappe.throw(_("Only a Machine image can be deleted."), exc=AtlasUserError)
		if image.status != "Available":
			frappe.throw(_("Only an Available Machine image can be deleted."), exc=AtlasUserError)
		self.validate_is_unused(cast(str, image.name))

		image.status = "Deleting"
		image.enabled = 0
		image.save()
		self.enqueue(cast(str, image.name))

	def enqueue(self, image_name: str) -> None:
		"""Enqueue one repeatable Machine image cleanup."""
		frappe.enqueue(
			"atlas.vm.core.vm_image_deletion.delete_virtual_machine_image",
			queue="long",
			timeout=DELETION_TIMEOUT_SECONDS,
			image_name=image_name,
			job_id=f"atlas||machine-image||deletion||{image_name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	def delete(self, image_name: str) -> None:
		"""Remove stored objects, incomplete uploads, and staged Metal data."""
		image = cast("VirtualMachineImage", frappe.get_doc("Virtual Machine Image", image_name))
		self.validate_is_unused(image_name)
		client = cast("AtlasSettings", frappe.get_single("Atlas Settings")).get_object_storage_client()
		self.abort_uploads(image, client)
		self.delete_objects(image, client)
		self.delete_staged_snapshot(image)
		frappe.delete_doc("Virtual Machine Image", image_name)

	@staticmethod
	def validate_is_unused(image_name: str) -> None:
		"""Reject deletion while a virtual machine uses the image."""
		if frappe.db.exists("Virtual Machine", {"virtual_machine_image": image_name}):
			frappe.throw(_("A Virtual Machine uses this image."), exc=VirtualMachineImageInUse)

	@staticmethod
	def abort_uploads(image: VirtualMachineImage, client: ObjectStorageClient) -> None:
		"""Abort each multipart upload that the transfer did not complete."""
		for object_key, upload_id in (
			(image.image_object_key, image.rootfs_multipart_upload_id),
			(image.kernel_object_key, image.kernel_multipart_upload_id),
		):
			if object_key and upload_id:
				client.abort_multipart_upload(object_key, upload_id)

	@staticmethod
	def delete_objects(image: VirtualMachineImage, client: ObjectStorageClient) -> None:
		"""Remove both stored artifacts."""
		for object_key in (image.image_object_key, image.kernel_object_key):
			if object_key:
				client.delete_object(object_key)

	@staticmethod
	def delete_staged_snapshot(image: VirtualMachineImage) -> None:
		"""Remove Metal staging data that a stopped transfer left behind."""
		if not image.source_server or not frappe.db.exists("Metal Server", image.source_server):
			return

		server = cast("MetalServer", frappe.get_doc("Metal Server", image.source_server))
		try:
			MetalClient(server).delete_snapshot(cast(str, image.name))
		except MetalClientError as error:
			if not error.is_not_found:
				raise


def enqueue_pending_virtual_machine_image_deletions() -> None:
	"""Resume Machine image deletions that did not finish."""
	names = frappe.get_all(
		"Virtual Machine Image",
		filters={"image_type": "machine", "status": "Deleting"},
		pluck="name",
	)
	service = VirtualMachineImageDeletionService()
	for name in names:
		service.enqueue(name)


@run_as_admin
def delete_virtual_machine_image(image_name: str) -> None:
	"""Run one queued Machine image cleanup and record a visible failure."""
	try:
		VirtualMachineImageDeletionService().delete(image_name)
	except (MetalClientError, ObjectStorageError) as error:
		frappe.db.set_value("Virtual Machine Image", image_name, "transfer_error", str(error)[:1000])
		frappe.log_error(
			title=f"Machine image deletion failed for {image_name}",
			message=frappe.get_traceback(),
		)
