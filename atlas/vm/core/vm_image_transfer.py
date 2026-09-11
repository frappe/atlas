from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

import frappe

from atlas.atlas.core.background_jobs import run_as_admin
from atlas.atlas.object_storage import ObjectStorageError
from atlas.vm.core.metal_client import MetalClient, MetalClientError
from atlas.vm.core.multipart_upload import MultipartUploadError, MultipartUploadService, bytes_to_mib
from atlas.vm.core.vm_service import VirtualMachineService

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.metal_server.doctype.metal_server.metal_server import MetalServer
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine
	from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage

TRANSFER_TIMEOUT_SECONDS = 900
ImageStatus = Literal["Pending", "Uploading", "Completing", "Cleaning", "Available", "Failed"]


class VirtualMachineImageTransferError(Exception):
	"""Report invalid Machine image transfer data."""


class VirtualMachineImageTransferService:
	"""Create and transfer one Machine image."""

	def create_from_virtual_machine(
		self,
		virtual_machine: VirtualMachine,
		title: str,
		*,
		image_type: str = "machine",
		cache_image: bool = False,
		memory_snapshot: bool = False,
	) -> str:
		"""Create a snapshot record and enqueue its transfer."""
		memory_snapshot_configuration = (
			(virtual_machine.vcpus, virtual_machine.memory_mib, virtual_machine.disk_mib)
			if memory_snapshot
			else (0, 0, 0)
		)
		original_image = cast(
			"VirtualMachineImage",
			frappe.get_doc("Virtual Machine Image", virtual_machine.virtual_machine_image),
		)
		server = cast("MetalServer", frappe.get_doc("Metal Server", virtual_machine.server))
		metal_client = MetalClient(server)
		try:
			snapshot = metal_client.create_snapshot(cast(str, virtual_machine.name))
		except MetalClientError as error:
			VirtualMachineService.raise_metal_error(error)

		snapshot_id = self.get_snapshot_id(snapshot)
		image = frappe.get_doc(
			{
				"doctype": "Virtual Machine Image",
				"title": title,
				"image_type": image_type,
				"tenant_id": virtual_machine.tenant_id,
				"status": "Pending",
				"enabled": 1,
				"platform": original_image.platform,
				"operating_system": original_image.operating_system,
				"operating_system_version": original_image.operating_system_version,
				"supports_cloud_init": original_image.supports_cloud_init,
				"cache_image": int(cache_image),
				"memory_snapshot": int(memory_snapshot),
				"memory_snapshot_virtual_cpu_count": memory_snapshot_configuration[0],
				"memory_snapshot_memory_mib": memory_snapshot_configuration[1],
				"memory_snapshot_disk_mib": memory_snapshot_configuration[2],
				"image_object_key": f"images/{snapshot_id}/rootfs.img",
				"image_size_mib": self.get_positive_size(snapshot, "rootfs"),
				"kernel_object_key": f"images/{snapshot_id}/kernel",
				"kernel_size_mib": self.get_positive_size(snapshot, "kernel"),
				"source_virtual_machine": virtual_machine.name,
				"source_server": virtual_machine.server,
			}
		)
		try:
			image.insert(set_name=snapshot_id)
		except Exception:
			self.delete_abandoned_snapshot(metal_client, snapshot_id)
			raise

		self.enqueue(snapshot_id)
		return snapshot_id

	def enqueue(
		self, image_name: str, *, queue: str = "default", timeout: int = TRANSFER_TIMEOUT_SECONDS
	) -> None:
		"""Enqueue one idempotent Machine image transfer."""
		frappe.enqueue(
			"atlas.vm.core.vm_image_transfer.transfer_machine_image",
			queue=queue,
			timeout=timeout,
			image_name=image_name,
			job_id=f"atlas||machine-image||{image_name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	def transfer(self, image_name: str) -> None:
		"""Advance one transfer and record a visible failure."""
		image = cast("VirtualMachineImage", frappe.get_doc("Virtual Machine Image", image_name))
		try:
			self.advance(image)
		except (
			MetalClientError,
			ObjectStorageError,
			MultipartUploadError,
			VirtualMachineImageTransferError,
		) as error:
			self.mark_failed(image, str(error))
			frappe.log_error(
				title=f"Machine image transfer failed for {image.name}",
				message=frappe.get_traceback(),
			)

	def advance(self, image: VirtualMachineImage) -> None:
		"""Move one Machine image transfer forward by one step."""
		server = cast(
			"MetalServer",
			frappe.get_doc("Metal Server", self.require_value(image.source_server, "source server")),
		)
		metal_client = MetalClient(server)
		object_storage_client = cast(
			"AtlasSettings", frappe.get_single("Atlas Settings")
		).get_object_storage_client()
		multipart_upload = MultipartUploadService(image, object_storage_client)

		if image.image_sha256 and image.kernel_sha256:
			self.finalize(image, metal_client, multipart_upload)
			return

		try:
			status = metal_client.get_snapshot(cast(str, image.name))
		except MetalClientError as error:
			if error.is_not_found:
				raise VirtualMachineImageTransferError(
					"Metal has no staged snapshot for this image"
				) from error
			raise

		state = status.get("state")
		if state == "completed":
			self.record_completed_upload(image, status)
		elif state == "failed":
			self.mark_failed(image, status.get("error") or "Metal reported an upload failure")
		elif state == "pending":
			self.start_upload(image, metal_client, multipart_upload)
		else:
			self.record_progress(image, status)

	def start_upload(
		self,
		image: VirtualMachineImage,
		metal_client: MetalClient,
		multipart_upload: MultipartUploadService,
	) -> None:
		"""Save retry identifiers before the Metal upload request."""
		image.status = "Uploading"
		image.transfer_progress = 0
		image.transfer_error = None
		multipart_upload.ensure_uploads(save=False)
		image.save()
		metal_client.start_snapshot_upload(cast(str, image.name), multipart_upload.get_upload_request())

	def finalize(
		self,
		image: VirtualMachineImage,
		metal_client: MetalClient,
		multipart_upload: MultipartUploadService,
	) -> None:
		"""Complete stored objects and remove the staged Metal snapshot."""
		self.update_status(image, "Completing")
		multipart_upload.complete_stored_uploads()
		self.update_status(image, "Cleaning")
		metal_client.delete_snapshot(cast(str, image.name))
		self.mark_available(image)

	@staticmethod
	def record_progress(image: VirtualMachineImage, status: dict[str, Any]) -> None:
		"""Store changed transfer progress."""
		percent = status.get("progress_percent")
		if isinstance(percent, int) and percent != image.transfer_progress:
			image.db_set("transfer_progress", max(0, min(100, percent)))

	def record_completed_upload(self, image: VirtualMachineImage, status: dict[str, Any]) -> None:
		"""Store checksums before finalization removes the snapshot."""
		image.image_sha256 = self.require_artifact_sha256(status, "rootfs")
		image.kernel_sha256 = self.require_artifact_sha256(status, "kernel")
		image.status = "Completing"
		image.transfer_progress = 100
		image.transfer_error = None
		image.save()

	@staticmethod
	def require_artifact_sha256(status: dict[str, Any], artifact: str) -> str:
		"""Return one valid artifact SHA-256 value."""
		value = status.get(artifact)
		sha256 = value.get("sha256") if isinstance(value, dict) else None
		if not isinstance(sha256, str) or len(sha256) != 64:
			raise VirtualMachineImageTransferError(f"Metal returned an invalid {artifact} SHA-256")
		if any(character not in "0123456789abcdef" for character in sha256):
			raise VirtualMachineImageTransferError(f"Metal returned an invalid {artifact} SHA-256")
		return sha256

	@staticmethod
	def get_snapshot_id(response: dict[str, Any]) -> str:
		"""Return the valid snapshot identifier from Metal."""
		snapshot_id = response.get("id")
		if not isinstance(snapshot_id, str) or not snapshot_id:
			raise VirtualMachineImageTransferError("Metal returned an invalid snapshot ID")
		return snapshot_id

	@staticmethod
	def get_positive_size(response: dict[str, Any], artifact: str) -> int:
		"""Return one positive artifact size in MiB."""
		value = response.get(artifact)
		size_bytes = value.get("size_bytes") if isinstance(value, dict) else None
		if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
			raise VirtualMachineImageTransferError(f"Metal returned an invalid {artifact} size")
		return bytes_to_mib(size_bytes)

	@staticmethod
	def require_value(value: str | None, label: str) -> str:
		"""Return one required transfer value."""
		if not value:
			raise VirtualMachineImageTransferError(f"Machine image has no {label}")
		return value

	@staticmethod
	def delete_abandoned_snapshot(metal_client: MetalClient, snapshot_id: str) -> None:
		"""Try to remove a snapshot after its Atlas insert fails."""
		try:
			metal_client.delete_snapshot(snapshot_id)
		except MetalClientError:
			frappe.log_error(
				title=f"Could not delete abandoned snapshot {snapshot_id}",
				message=frappe.get_traceback(),
			)

	@staticmethod
	def update_status(image: VirtualMachineImage, status: ImageStatus) -> None:
		"""Save one active transfer status."""
		image.status = status
		image.transfer_error = None
		image.save()

	@staticmethod
	def mark_available(image: VirtualMachineImage) -> None:
		"""Mark an image available and clear completed upload identifiers."""
		image.status = "Available"
		image.rootfs_multipart_upload_id = None
		image.kernel_multipart_upload_id = None
		image.transfer_error = None
		image.save()

	@staticmethod
	def mark_failed(image: VirtualMachineImage, message: str) -> None:
		"""Store a transfer error without clearing retry identifiers."""
		image.status = "Failed"
		image.transfer_error = message[:1000]
		image.save()


def enqueue_pending_machine_image_transfers() -> None:
	"""Resume Machine image transfers that did not finish."""
	names = frappe.get_all(
		"Virtual Machine Image",
		filters={
			"image_type": "machine",
			"status": ["in", ["Pending", "Uploading", "Completing", "Cleaning"]],
		},
		pluck="name",
	)
	service = VirtualMachineImageTransferService()
	for name in names:
		service.enqueue(name)


@run_as_admin
def transfer_machine_image(image_name: str) -> None:
	"""Advance one queued Machine image transfer."""
	VirtualMachineImageTransferService().transfer(image_name)
