from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe import _
from frappe.model.document import Document

SIGNED_URL_EXPIRY_SECONDS = 86400
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings


class VirtualMachineImage(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cache_image: DF.Check
		enabled: DF.Check
		image_object_key: DF.Data | None
		image_sha256: DF.Data | None
		image_size_mib: DF.Int
		image_type: DF.Literal["System", "Machine"]
		kernel_multipart_upload_id: DF.Data | None
		kernel_object_key: DF.Data | None
		kernel_sha256: DF.Data | None
		kernel_size_mib: DF.Int
		memory_snapshot: DF.Check
		memory_snapshot_disk_mib: DF.Int
		memory_snapshot_memory_mib: DF.Int
		memory_snapshot_virtual_cpu_count: DF.Int
		operating_system: DF.Data
		operating_system_version: DF.Data
		platform: DF.Literal["amd64", "arm64"]
		rootfs_multipart_upload_id: DF.Data | None
		source_local_snapshot_id: DF.Data | None
		source_server: DF.Data | None
		source_virtual_machine: DF.Data | None
		status: DF.Literal[
			"Pending", "Snapshotting", "Uploading", "Completing", "Cleaning", "Available", "Failed"
		]
		supports_cloud_init: DF.Check
		title: DF.Data
		transfer_error: DF.SmallText | None
		transfer_progress: DF.Int
		version: DF.Int
	# end: auto-generated types

	def validate(self) -> None:
		self.validate_memory_snapshot_configuration()
		if self.status == "Available":
			self.validate_artifacts()

	def get_metal_image_request(self, user_data: str = "") -> dict[str, Any]:
		self.validate_user_data(user_data)
		self.validate_is_available()
		if self.cache_image:
			return self.get_desired_image()
		return self.get_metal_image(SIGNED_URL_EXPIRY_SECONDS)

	def get_desired_image(self) -> dict[str, Any]:
		image = self.get_metal_image(SIGNED_URL_EXPIRY_SECONDS)
		image.update(
			{
				"cache_image": bool(self.cache_image),
				"memory_snapshot": bool(self.memory_snapshot),
				"memory_snapshot_configuration": self.memory_snapshot_configuration,
			}
		)
		return image

	def get_metal_image(self, expiry_seconds: int) -> dict[str, Any]:
		return {
			"ref": self.immutable_reference,
			"architecture": self.platform,
			"rootfs": {"url": self.get_image_url(expiry_seconds), "sha256": self.image_sha256},
			"kernel": {"url": self.get_kernel_url(expiry_seconds), "sha256": self.kernel_sha256},
		}

	@property
	def immutable_reference(self) -> str:
		identity = f"{self.platform}\0{self.image_sha256}\0{self.kernel_sha256}"
		return f"sha256:{hashlib.sha256(identity.encode()).hexdigest()}"

	@property
	def memory_snapshot_configuration(self) -> dict[str, int] | None:
		if not self.memory_snapshot:
			return None
		return {
			"virtual_cpu_count": self.memory_snapshot_virtual_cpu_count,
			"memory_mib": self.memory_snapshot_memory_mib,
			"disk_mib": self.memory_snapshot_disk_mib,
		}

	def get_image_url(self, expiry_seconds: int = SIGNED_URL_EXPIRY_SECONDS) -> str:
		return self.get_object_url(self.image_object_key, expiry_seconds)

	def get_kernel_url(self, expiry_seconds: int = SIGNED_URL_EXPIRY_SECONDS) -> str:
		return self.get_object_url(self.kernel_object_key, expiry_seconds)

	def validate_memory_snapshot_configuration(self) -> None:
		if not self.memory_snapshot:
			return

		fields = (
			("memory_snapshot_virtual_cpu_count", _("Memory Snapshot vCPUs")),
			("memory_snapshot_memory_mib", _("Memory Snapshot Memory")),
			("memory_snapshot_disk_mib", _("Memory Snapshot Disk")),
		)
		for fieldname, label in fields:
			value = self.get(fieldname)
			if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
				frappe.throw(_("{0} must be a positive integer.").format(label))

		if self.image_size_mib and self.memory_snapshot_disk_mib < self.image_size_mib:
			frappe.throw(_("Memory Snapshot Disk must be at least {0} MiB.").format(self.image_size_mib))

	def validate_artifacts(self) -> None:
		if not self.image_object_key or not self.kernel_object_key:
			frappe.throw(_("An available Virtual Machine Image requires rootfs and kernel object keys."))
		if not SHA256_PATTERN.fullmatch(self.image_sha256 or ""):
			frappe.throw(_("Image SHA-256 must contain 64 lowercase hexadecimal characters."))
		if not SHA256_PATTERN.fullmatch(self.kernel_sha256 or ""):
			frappe.throw(_("Kernel SHA-256 must contain 64 lowercase hexadecimal characters."))
		if self.image_size_mib <= 0 or self.kernel_size_mib <= 0:
			frappe.throw(_("An available Virtual Machine Image requires positive artifact sizes."))

	def validate_is_available(self) -> None:
		if self.status != "Available":
			frappe.throw(_("Virtual Machine Image {0} is not available.").format(self.title))

	def validate_compatibility(self, disk_mib: int) -> None:
		"""Check that the requested disk can hold the image."""
		if disk_mib < self.image_size_mib:
			frappe.throw(
				_("Disk must be at least {0} MiB for image {1}.").format(self.image_size_mib, self.title)
			)

	def validate_user_data(self, user_data: str) -> None:
		if user_data and not self.supports_cloud_init:
			frappe.throw(_("This Virtual Machine Image does not support cloud-init user data."))

	def get_object_url(self, object_key: str | None, expiry_seconds: int) -> str:
		if not object_key:
			frappe.throw(_("Virtual Machine Image {0} has no object key.").format(self.title))
		settings = cast("AtlasSettings", frappe.get_single("Atlas Settings"))
		return settings.get_s3_client().object_url(object_key, expiry_seconds=expiry_seconds)

	@frappe.whitelist(methods=["POST"])
	def retry_transfer(self) -> None:
		frappe.only_for("System Manager")
		if self.image_type != "Machine" or self.status != "Failed":
			frappe.throw(_("Only a failed Machine image transfer can be retried."))

		frappe.enqueue(
			"atlas.vm.core.virtual_machine_image_manager.transfer_machine_image",
			queue="long",
			timeout=7200,
			image_name=self.name,
			job_id=f"atlas||machine-image||{self.name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)
