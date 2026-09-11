from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, now_datetime

from atlas.atlas.core.exceptions import AtlasUserError

SIGNED_URL_EXPIRY_SECONDS = 86400
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ImageDownload(TypedDict):
	"""One signed image artifact download."""

	artifact: Literal["rootfs", "kernel"]
	url: str
	size_mib: int
	sha256: str
	expires_in: int
	expires_at: str


if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings


class VirtualMachineImage(Document):
	"""One durable boot artifact. Its reference is immutable."""

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
		image_type: DF.Literal["system", "machine"]
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
			"Pending",
			"Snapshotting",
			"Uploading",
			"Completing",
			"Cleaning",
			"Available",
			"Failed",
			"Deleting",
		]
		supports_cloud_init: DF.Check
		tenant_id: DF.Int
		title: DF.Data
		transfer_error: DF.SmallText | None
		transfer_progress: DF.Int
		version: DF.Int
	# end: auto-generated types

	def validate(self) -> None:
		"""Reject an image whose artifacts or snapshot shape are inconsistent."""
		self.validate_memory_snapshot_configuration()
		if self.status == "Available":
			self.validate_artifacts()

	def get_metal_image_request(self, user_data: str = "") -> dict[str, Any]:
		"""Return the image object for a Metal create request."""
		self.validate_user_data(user_data)
		self.validate_is_available()
		if self.cache_image:
			return self.get_desired_image()
		return self.get_metal_image(SIGNED_URL_EXPIRY_SECONDS)

	def get_desired_image(self) -> dict[str, Any]:
		"""Return the image policy this host should cache."""
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
		"""Return the image object with freshly signed artifact URLs."""
		return {
			"ref": self.immutable_reference,
			"architecture": self.platform,
			"rootfs": {"url": self.get_presigned_image_url(expiry_seconds), "sha256": self.image_sha256},
			"kernel": {"url": self.get_presigned_kernel_url(expiry_seconds), "sha256": self.kernel_sha256},
		}

	@property
	def is_shared(self) -> bool:
		"""Return whether every tenant can read and boot this image."""
		return self.image_type == "system"

	def is_visible_to_tenant(self, tenant_id: int) -> bool:
		"""Return whether one tenant can read and boot this image."""
		return self.is_shared or self.tenant_id == tenant_id

	@property
	def immutable_reference(self) -> str:
		"""Return the name that identifies this exact content on a host."""
		identity = f"{self.platform}\0{self.image_sha256}\0{self.kernel_sha256}"
		return f"sha256:{hashlib.sha256(identity.encode()).hexdigest()}"

	@property
	def memory_snapshot_configuration(self) -> dict[str, int] | None:
		"""Return the exact VM shape a warm artifact serves."""
		if not self.memory_snapshot:
			return None
		return {
			"virtual_cpu_count": self.memory_snapshot_virtual_cpu_count,
			"memory_mib": self.memory_snapshot_memory_mib,
			"disk_mib": self.memory_snapshot_disk_mib,
		}

	def get_presigned_download_url(self, artifact: Literal["rootfs", "kernel"]) -> ImageDownload:
		"""Return one signed artifact URL with its size, digest, and expiry time."""
		self.validate_is_available()
		if artifact == "rootfs":
			url = self.get_presigned_image_url()
			size_mib = self.image_size_mib
			sha256 = self.image_sha256
		else:
			url = self.get_presigned_kernel_url()
			size_mib = self.kernel_size_mib
			sha256 = self.kernel_sha256

		return {
			"artifact": artifact,
			"url": url,
			"size_mib": size_mib,
			"sha256": sha256,
			"expires_in": SIGNED_URL_EXPIRY_SECONDS,
			"expires_at": str(add_to_date(now_datetime(), seconds=SIGNED_URL_EXPIRY_SECONDS)),
		}

	def get_presigned_image_url(self, expiry_seconds: int = SIGNED_URL_EXPIRY_SECONDS) -> str:
		"""Return a signed URL for the root file system object."""
		return self.get_object_url(self.image_object_key, expiry_seconds)

	def get_presigned_kernel_url(self, expiry_seconds: int = SIGNED_URL_EXPIRY_SECONDS) -> str:
		"""Return a signed URL for the kernel object."""
		return self.get_object_url(self.kernel_object_key, expiry_seconds)

	def validate_memory_snapshot_configuration(self) -> None:
		"""Require a complete VM shape when a warm artifact is requested."""
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
		"""Require both artifacts with an exact size and digest."""
		if not self.image_object_key or not self.kernel_object_key:
			frappe.throw(_("An available Virtual Machine Image requires rootfs and kernel object keys."))
		if not SHA256_PATTERN.fullmatch(self.image_sha256 or ""):
			frappe.throw(_("Image SHA-256 must contain 64 lowercase hexadecimal characters."))
		if not SHA256_PATTERN.fullmatch(self.kernel_sha256 or ""):
			frappe.throw(_("Kernel SHA-256 must contain 64 lowercase hexadecimal characters."))
		if self.image_size_mib <= 0 or self.kernel_size_mib <= 0:
			frappe.throw(_("An available Virtual Machine Image requires positive artifact sizes."))

	def validate_is_available(self) -> None:
		"""Reject an image that is not ready to boot a VM."""
		if self.status != "Available":
			frappe.throw(
				_("Virtual Machine Image {0} is not available.").format(self.title), exc=AtlasUserError
			)

	def validate_compatibility(self, disk_mib: int) -> None:
		"""Check that the requested disk can hold the image."""
		if disk_mib < self.image_size_mib:
			frappe.throw(
				_("Disk must be at least {0} MiB for image {1}.").format(self.image_size_mib, self.title),
				exc=AtlasUserError,
			)

	def validate_user_data(self, user_data: str) -> None:
		"""Reject user data that the guest cannot accept."""
		if user_data and not self.supports_cloud_init:
			frappe.throw(
				_("This Virtual Machine Image does not support cloud-init user data."), exc=AtlasUserError
			)

	def get_object_url(self, object_key: str | None, expiry_seconds: int) -> str:
		"""Return a signed URL for one stored object."""
		if not object_key:
			frappe.throw(_("Virtual Machine Image {0} has no object key.").format(self.title))
		settings = cast("AtlasSettings", frappe.get_single("Atlas Settings"))
		return settings.get_object_storage_client().object_url(object_key, expiry_seconds=expiry_seconds)

	@frappe.whitelist(methods=["POST"])
	def retry_transfer(self) -> None:
		"""Start the image transfer again, keeping the existing identifiers."""
		self.check_permission("write")
		if self.image_type != "machine" or self.status != "Failed":
			frappe.throw(_("Only a failed Machine image transfer can be retried."))

		from atlas.vm.core.vm_image_transfer import VirtualMachineImageTransferService

		VirtualMachineImageTransferService().enqueue(self.name, queue="long", timeout=7200)

	@frappe.whitelist(methods=["POST"])
	def request_deletion(self) -> None:
		"""Queue deletion of this unused Machine image."""
		self.check_permission("write")

		from atlas.vm.core.vm_image_deletion import VirtualMachineImageDeletionService

		VirtualMachineImageDeletionService().request(self)
