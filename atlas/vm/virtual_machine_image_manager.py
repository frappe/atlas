from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

import frappe
from frappe import _

from atlas.atlas.s3 import S3Client, S3Error
from atlas.vm.metal_client import MetalClient, MetalClientError, throw_metal_error

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings
	from atlas.server.doctype.server.server import Server
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine
	from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage

MEBIBYTE = 1 << 20
MULTIPART_PART_SIZE_MIB = 2 * 1024
TRANSFER_TIMEOUT_SECONDS = 7200
ImageStatus = Literal["Pending", "Uploading", "Completing", "Cleaning", "Available", "Failed"]


class VirtualMachineImageTransferError(Exception):
	"""Report invalid image transfer data."""


class VirtualMachineImageManager:
	"""Create and transfer Machine images."""

	def create_from_virtual_machine(
		self,
		virtual_machine: VirtualMachine,
		title: str,
		*,
		cache_image: bool = False,
		memory_snapshot: bool = False,
		memory_snapshot_virtual_cpu_count: int = 0,
		memory_snapshot_memory_mib: int = 0,
		memory_snapshot_disk_mib: int = 0,
	) -> str:
		memory_snapshot_configuration = self.validate_memory_snapshot_configuration(
			memory_snapshot,
			memory_snapshot_virtual_cpu_count,
			memory_snapshot_memory_mib,
			memory_snapshot_disk_mib,
			minimum_disk_mib=virtual_machine.disk_mib,
		)
		original_image = cast(
			"VirtualMachineImage",
			frappe.get_doc("Virtual Machine Image", virtual_machine.virtual_machine_image),
		)
		server = cast("Server", frappe.get_doc("Server", virtual_machine.server))
		metal_client = MetalClient(server)
		try:
			snapshot = metal_client.create_snapshot(cast(str, virtual_machine.name))
		except MetalClientError as error:
			throw_metal_error(error)
			raise AssertionError from error

		snapshot_id = self.get_snapshot_id(snapshot)
		image = frappe.get_doc(
			{
				"doctype": "Virtual Machine Image",
				"title": title,
				"image_type": "Machine",
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
			image.insert(ignore_permissions=True, set_name=snapshot_id)
		except Exception:
			self.delete_abandoned_snapshot(metal_client, snapshot_id)
			raise

		self.enqueue_transfer(snapshot_id)
		return snapshot_id

	@staticmethod
	def validate_memory_snapshot_configuration(
		memory_snapshot: bool,
		virtual_cpu_count: int,
		memory_mib: int,
		disk_mib: int,
		*,
		minimum_disk_mib: int,
	) -> tuple[int, int, int]:
		if not memory_snapshot:
			return 0, 0, 0
		if virtual_cpu_count <= 0 or memory_mib <= 0 or disk_mib <= 0:
			frappe.throw(_("Memory snapshot CPU, memory, and disk values must be positive."))
		if disk_mib < minimum_disk_mib:
			frappe.throw(_("Memory snapshot disk must be at least {0} MiB.").format(minimum_disk_mib))
		return virtual_cpu_count, memory_mib, disk_mib

	def enqueue_transfer(self, image_name: str) -> None:
		frappe.enqueue(
			"atlas.vm.virtual_machine_image_manager.transfer_machine_image",
			queue="long",
			timeout=TRANSFER_TIMEOUT_SECONDS,
			image_name=image_name,
			job_id=f"atlas||machine-image||{image_name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	def transfer(self, image_name: str) -> None:
		image = cast("VirtualMachineImage", frappe.get_doc("Virtual Machine Image", image_name))
		try:
			self.perform_transfer(image)
		except (MetalClientError, S3Error, VirtualMachineImageTransferError) as error:
			self.mark_failed(image, str(error))
			frappe.log_error(
				title=f"Machine image transfer failed for {image.name}",
				message=frappe.get_traceback(),
			)

	def perform_transfer(self, image: VirtualMachineImage) -> None:
		server_name = self.require_value(image.source_server, "source server")
		snapshot_id = cast(str, image.name)
		server = cast("Server", frappe.get_doc("Server", server_name))
		metal_client = MetalClient(server)
		settings = cast("AtlasSettings", frappe.get_single("Atlas Settings"))
		s3_client = settings.get_s3_client()

		if image.image_sha256 and image.kernel_sha256:
			self.complete_stored_uploads(image, s3_client)
		else:
			self.upload_snapshot(image, metal_client, s3_client)

		self.update_status(image, "Cleaning")
		metal_client.delete_snapshot(snapshot_id)
		self.mark_available(image)

	def upload_snapshot(
		self, image: VirtualMachineImage, metal_client: MetalClient, s3_client: S3Client
	) -> None:
		snapshot_id = cast(str, image.name)
		self.update_status(image, "Uploading")
		self.ensure_multipart_uploads(image, s3_client)
		upload_request = self.get_upload_request(image, s3_client)
		upload_response = metal_client.upload_snapshot(snapshot_id, upload_request)
		parts_by_artifact = self.validate_upload_response(image, upload_response)

		image.image_sha256 = upload_response["rootfs"]["sha256"]
		image.kernel_sha256 = upload_response["kernel"]["sha256"]
		image.status = "Completing"
		image.transfer_error = None
		image.save(ignore_permissions=True)
		frappe.db.commit()
		self.complete_uploads(image, s3_client, parts_by_artifact)

	def complete_stored_uploads(self, image: VirtualMachineImage, s3_client: S3Client) -> None:
		self.update_status(image, "Completing")

		parts_by_artifact = {
			"rootfs": self.get_stored_parts(
				s3_client,
				self.require_value(image.image_object_key, "rootfs object key"),
				self.require_value(image.rootfs_multipart_upload_id, "rootfs upload ID"),
				image.image_size_mib,
			),
			"kernel": self.get_stored_parts(
				s3_client,
				self.require_value(image.kernel_object_key, "kernel object key"),
				self.require_value(image.kernel_multipart_upload_id, "kernel upload ID"),
				image.kernel_size_mib,
			),
		}
		self.complete_uploads(image, s3_client, parts_by_artifact)

	def get_stored_parts(
		self, s3_client: S3Client, object_key: str, upload_id: str, size_mib: int
	) -> list[dict[str, Any]]:
		head = s3_client.head_object(object_key)
		if head and self.has_expected_size(head, size_mib):
			return []
		parts = s3_client.list_multipart_parts(object_key, upload_id)
		self.validate_parts("stored", size_mib, parts)
		return parts

	@staticmethod
	def get_snapshot_id(response: dict[str, Any]) -> str:
		snapshot_id = response.get("id")
		if not isinstance(snapshot_id, str) or not snapshot_id:
			raise VirtualMachineImageTransferError("Metal returned an invalid snapshot ID")
		return snapshot_id

	@staticmethod
	def delete_abandoned_snapshot(metal_client: MetalClient, snapshot_id: str) -> None:
		try:
			metal_client.delete_snapshot(snapshot_id)
		except MetalClientError:
			frappe.log_error(
				title=f"Could not delete abandoned snapshot {snapshot_id}",
				message=frappe.get_traceback(),
			)

	def ensure_multipart_uploads(self, image: VirtualMachineImage, s3_client: S3Client) -> None:
		if not image.rootfs_multipart_upload_id:
			object_key = self.require_value(image.image_object_key, "rootfs object key")
			image.rootfs_multipart_upload_id = s3_client.create_multipart_upload(object_key)
			image.save(ignore_permissions=True)
			frappe.db.commit()

		if not image.kernel_multipart_upload_id:
			object_key = self.require_value(image.kernel_object_key, "kernel object key")
			image.kernel_multipart_upload_id = s3_client.create_multipart_upload(object_key)
			image.save(ignore_permissions=True)
			frappe.db.commit()

	def get_upload_request(self, image: VirtualMachineImage, s3_client: S3Client) -> dict[str, Any]:
		return {
			"rootfs": {
				"parts": self.get_signed_parts(
					s3_client,
					self.require_value(image.image_object_key, "rootfs object key"),
					self.require_value(image.rootfs_multipart_upload_id, "rootfs upload ID"),
					image.image_size_mib,
				)
			},
			"kernel": {
				"parts": self.get_signed_parts(
					s3_client,
					self.require_value(image.kernel_object_key, "kernel object key"),
					self.require_value(image.kernel_multipart_upload_id, "kernel upload ID"),
					image.kernel_size_mib,
				)
			},
		}

	def get_signed_parts(
		self, s3_client: S3Client, object_key: str, upload_id: str, size_mib: int
	) -> list[dict[str, int | str]]:
		return [
			{
				"part_number": part_number,
				"url": s3_client.sign_upload_part(object_key, upload_id, part_number, expiry_seconds=86400),
			}
			for part_number in range(1, get_multipart_part_count(size_mib) + 1)
		]

	def validate_upload_response(
		self, image: VirtualMachineImage, response: dict[str, Any]
	) -> dict[str, list[dict[str, Any]]]:
		parts_by_artifact = {}
		for artifact, expected_mib in (
			("rootfs", image.image_size_mib),
			("kernel", image.kernel_size_mib),
		):
			value = response.get(artifact)
			if not isinstance(value, dict):
				raise VirtualMachineImageTransferError(f"Metal returned invalid {artifact} data")

			size_bytes = value.get("size_bytes")
			if not isinstance(size_bytes, int) or bytes_to_mib(size_bytes) != expected_mib:
				raise VirtualMachineImageTransferError(f"Metal returned an invalid {artifact} size")
			self.validate_sha256(artifact, value.get("sha256"))
			parts = value.get("parts")
			if not isinstance(parts, list):
				raise VirtualMachineImageTransferError(f"Metal returned invalid {artifact} parts")
			self.validate_parts(artifact, expected_mib, parts)
			parts_by_artifact[artifact] = parts
		return parts_by_artifact

	@staticmethod
	def validate_sha256(artifact: str, sha256: object) -> None:
		if not isinstance(sha256, str) or len(sha256) != 64:
			raise VirtualMachineImageTransferError(f"Metal returned an invalid {artifact} SHA-256")
		if any(character not in "0123456789abcdef" for character in sha256):
			raise VirtualMachineImageTransferError(f"Metal returned an invalid {artifact} SHA-256")

	def validate_parts(self, artifact: str, size_mib: int, parts: list[dict[str, Any]]) -> None:
		expected_numbers = list(range(1, get_multipart_part_count(size_mib) + 1))
		part_numbers = [part.get("part_number", part.get("PartNumber")) for part in parts]
		if part_numbers != expected_numbers:
			raise VirtualMachineImageTransferError(f"Metal returned invalid {artifact} part numbers")
		if any(not part.get("etag", part.get("ETag")) for part in parts):
			raise VirtualMachineImageTransferError(f"Metal returned an empty {artifact} ETag")

	def complete_uploads(
		self,
		image: VirtualMachineImage,
		s3_client: S3Client,
		parts_by_artifact: dict[str, list[dict[str, Any]]],
	) -> None:
		self.complete_upload(
			s3_client,
			self.require_value(image.image_object_key, "rootfs object key"),
			self.require_value(image.rootfs_multipart_upload_id, "rootfs upload ID"),
			image.image_size_mib,
			parts_by_artifact["rootfs"],
		)
		self.complete_upload(
			s3_client,
			self.require_value(image.kernel_object_key, "kernel object key"),
			self.require_value(image.kernel_multipart_upload_id, "kernel upload ID"),
			image.kernel_size_mib,
			parts_by_artifact["kernel"],
		)

	def complete_upload(
		self,
		s3_client: S3Client,
		object_key: str,
		upload_id: str,
		size_mib: int,
		parts: list[dict[str, Any]],
	) -> None:
		head = s3_client.head_object(object_key)
		if head and self.has_expected_size(head, size_mib):
			return
		s3_client.complete_multipart_upload(object_key, upload_id, parts)
		head = s3_client.head_object(object_key)
		if not head or not self.has_expected_size(head, size_mib):
			raise VirtualMachineImageTransferError(f"S3 stored an invalid size for {object_key}")

	@staticmethod
	def has_expected_size(head: dict[str, Any], size_mib: int) -> bool:
		content_length = head.get("ContentLength")
		return isinstance(content_length, int) and bytes_to_mib(content_length) == size_mib

	@staticmethod
	def get_positive_size(response: dict[str, Any], artifact: str) -> int:
		value = response.get(artifact)
		size_bytes = value.get("size_bytes") if isinstance(value, dict) else None
		if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
			raise VirtualMachineImageTransferError(f"Metal returned an invalid {artifact} size")
		return bytes_to_mib(size_bytes)

	@staticmethod
	def require_value(value: str | None, label: str) -> str:
		if not value:
			raise VirtualMachineImageTransferError(f"Machine image has no {label}")
		return value

	@staticmethod
	def update_status(image: VirtualMachineImage, status: ImageStatus) -> None:
		image.status = status
		image.transfer_error = None
		image.save(ignore_permissions=True)
		frappe.db.commit()

	@staticmethod
	def mark_available(image: VirtualMachineImage) -> None:
		image.status = "Available"
		image.rootfs_multipart_upload_id = None
		image.kernel_multipart_upload_id = None
		image.transfer_error = None
		image.save(ignore_permissions=True)
		frappe.db.commit()

	@staticmethod
	def mark_failed(image: VirtualMachineImage, message: str) -> None:
		image.status = "Failed"
		image.transfer_error = message[:1000]
		image.save(ignore_permissions=True)
		frappe.db.commit()


def bytes_to_mib(size_bytes: int) -> int:
	"""Return the size in MiB, rounded up to the next whole MiB."""
	return (size_bytes + MEBIBYTE - 1) // MEBIBYTE


def get_multipart_part_count(size_mib: int) -> int:
	if size_mib <= 0:
		raise VirtualMachineImageTransferError("Artifact size must be positive")
	part_count = (size_mib + MULTIPART_PART_SIZE_MIB - 1) // MULTIPART_PART_SIZE_MIB
	if part_count > 10_000:
		raise VirtualMachineImageTransferError("Artifact needs more than 10,000 multipart upload parts")
	return part_count


def enqueue_pending_machine_image_transfers() -> None:
	"""Resume Machine image transfers that did not finish."""
	names = frappe.get_all(
		"Virtual Machine Image",
		filters={
			"image_type": "Machine",
			"status": ["in", ["Pending", "Uploading", "Completing", "Cleaning"]],
		},
		pluck="name",
	)
	manager = VirtualMachineImageManager()
	for name in names:
		manager.enqueue_transfer(name)


def transfer_machine_image(image_name: str) -> None:
	VirtualMachineImageManager().transfer(image_name)
