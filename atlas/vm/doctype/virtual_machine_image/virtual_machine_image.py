from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, now_datetime

from atlas.atlas.core.artifacts import get_download_url
from atlas.atlas.core.exceptions import AtlasConflictError, AtlasUserError
from atlas.atlas.core.parsing import strict_bool
from atlas.atlas.core.tags import validate_tags
from atlas.vm.core.models import MAXIMUM_CPU_MILLICORES

SIGNED_URL_EXPIRY_SECONDS = 86400
# Firecracker exposes whole vCPUs, so a warm image shape is capped in cores.
MAXIMUM_SNAPSHOT_VIRTUAL_CPU_COUNT = MAXIMUM_CPU_MILLICORES // 1000
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

Artifact = Literal["rootfs", "kernel", "initrd"]


class ImageDownload(TypedDict):
	"""One signed image artifact download."""

	artifact: Artifact
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

		from atlas.atlas.doctype.atlas_tag.atlas_tag import AtlasTag

		architecture: DF.Literal["amd64", "arm64"]
		artifact_retention_until: DF.Datetime | None
		artifact_storage: DF.Literal["Object Storage", "Site File"]
		cache_image: DF.Check
		enabled: DF.Check
		image_file: DF.Link | None
		image_object_key: DF.Data | None
		image_sha256: DF.Data | None
		image_size_mib: DF.Int
		image_stored_size_mib: DF.Int
		image_type: DF.Literal["system", "machine"]
		initrd_file: DF.Link | None
		initrd_multipart_upload_id: DF.Data | None
		initrd_object_key: DF.Data | None
		initrd_sha256: DF.Data | None
		initrd_size_mib: DF.Int
		initrd_stored_size_mib: DF.Int
		is_termination_protected: DF.Check
		kernel_file: DF.Link | None
		kernel_multipart_upload_id: DF.Data | None
		kernel_object_key: DF.Data | None
		kernel_sha256: DF.Data | None
		kernel_size_mib: DF.Int
		kernel_stored_size_mib: DF.Int
		memory_snapshot: DF.Check
		memory_snapshot_disk_mib: DF.Int
		memory_snapshot_memory_mib: DF.Int
		memory_snapshot_virtual_cpu_count: DF.Int
		rootfs_multipart_upload_id: DF.Data | None
		site_file_retention_until: DF.Datetime | None
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
			"Archived",
		]
		tags: DF.Table[AtlasTag]
		tenant_id: DF.Int
		title: DF.Data
		transfer_error: DF.SmallText | None
		transfer_progress: DF.Int
		version: DF.Int
	# end: auto-generated types

	def validate(self) -> None:
		"""Reject an image whose artifacts, tags, or snapshot shape are inconsistent."""
		validate_tags(self)
		self.validate_memory_snapshot_configuration()
		if self.status == "Available":
			self.validate_artifacts()

	def before_insert(self) -> None:
		"""Protect a System image, because the Atlas services boot from one."""
		if self.image_type == "system":
			self.is_termination_protected = 1

	def on_trash(self) -> None:
		"""Keep a retired image record until cleanup and VM removal finish."""
		self.ensure_not_termination_protected()
		if self.status != "Deleting":
			frappe.throw(_("Retire the image before deleting its record."), exc=AtlasConflictError)

		from atlas.vm.core.vm_image_deletion import has_stored_artifacts

		if has_stored_artifacts(self):
			frappe.throw(_("Remove the image artifacts before deleting its record."), exc=AtlasConflictError)
		if frappe.db.exists("Virtual Machine", {"virtual_machine_image": self.name}):
			frappe.throw(_("A Virtual Machine still refers to this image."), exc=AtlasConflictError)

	def get_metal_image_request(self) -> dict[str, Any]:
		"""Return the image object for a Metal create request."""
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
		image = {
			"ref": self.immutable_reference,
			"architecture": self.architecture,
			"rootfs": {"url": self.get_artifact_url("rootfs", expiry_seconds), "sha256": self.image_sha256},
			"kernel": {"url": self.get_artifact_url("kernel", expiry_seconds), "sha256": self.kernel_sha256},
		}
		if self.has_initrd:
			image["initrd"] = {
				"url": self.get_artifact_url("initrd", expiry_seconds),
				"sha256": self.initrd_sha256,
			}
		return image

	@property
	def is_shared(self) -> bool:
		"""Return whether every tenant can read and boot this image."""
		return self.image_type == "system"

	@property
	def has_initrd(self) -> bool:
		"""Return whether this image includes an initrd artifact."""
		return bool(self.get("initrd_sha256"))

	def is_visible_to_tenant(self, tenant_id: int) -> bool:
		"""Return whether one tenant can read and boot this image."""
		return self.is_shared or self.tenant_id == tenant_id

	@property
	def immutable_reference(self) -> str:
		"""Return the name that identifies this exact content on a host."""
		identity = f"{self.architecture}\0{self.image_sha256}\0{self.kernel_sha256}"
		if initrd_sha256 := self.get("initrd_sha256"):
			identity = f"{identity}\0{initrd_sha256}"
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

	@frappe.whitelist()
	def get_presigned_download_url(self, artifact: Artifact) -> ImageDownload:
		"""Return one signed artifact URL with its size, digest, and expiry time."""
		if artifact not in ("rootfs", "kernel", "initrd"):
			frappe.throw(_("Artifact must be rootfs or kernel, or initrd."))

		self.validate_is_available()
		if artifact == "initrd" and not self.has_initrd:
			frappe.throw(_("Virtual Machine Image {0} has no initrd.").format(self.title))
		if self.is_stored_in_site_file:
			frappe.throw(
				_("Virtual Machine Image {0} is not in object storage.").format(self.title),
				exc=AtlasConflictError,
			)

		size_mib = {
			"rootfs": self.image_size_mib,
			"kernel": self.kernel_size_mib,
			"initrd": self.get("initrd_size_mib"),
		}[artifact]
		sha256 = {
			"rootfs": self.image_sha256,
			"kernel": self.kernel_sha256,
			"initrd": self.get("initrd_sha256"),
		}[artifact]
		return {
			"artifact": artifact,
			"url": self.get_artifact_url(artifact),
			"size_mib": size_mib,
			"sha256": sha256,
			"expires_in": SIGNED_URL_EXPIRY_SECONDS,
			"expires_at": str(add_to_date(now_datetime(), seconds=SIGNED_URL_EXPIRY_SECONDS)),
		}

	@property
	def is_stored_in_site_file(self) -> bool:
		"""Return whether the artifacts are public site files instead of objects."""
		return self.artifact_storage == "Site File"

	def get_artifact_url(self, artifact: Artifact, expiry_seconds: int = SIGNED_URL_EXPIRY_SECONDS) -> str:
		"""Return the download URL for one artifact."""
		if artifact == "initrd" and not self.has_initrd:
			frappe.throw(_("Virtual Machine Image {0} has no initrd.").format(self.title))
		if self.is_stored_in_site_file:
			file_name = {
				"rootfs": self.image_file,
				"kernel": self.kernel_file,
				"initrd": self.get("initrd_file"),
			}[artifact]
			if not file_name:
				frappe.throw(_("Virtual Machine Image {0} has no {1} file.").format(self.title, artifact))
			return get_download_url(file_name)

		object_key = {
			"rootfs": self.image_object_key,
			"kernel": self.kernel_object_key,
			"initrd": self.get("initrd_object_key"),
		}[artifact]
		return self.get_object_url(object_key, expiry_seconds)

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

		if self.memory_snapshot_virtual_cpu_count > MAXIMUM_SNAPSHOT_VIRTUAL_CPU_COUNT:
			frappe.throw(
				_("Memory Snapshot vCPUs must not exceed {0}.").format(MAXIMUM_SNAPSHOT_VIRTUAL_CPU_COUNT)
			)

		if self.image_size_mib and self.memory_snapshot_disk_mib < self.image_size_mib:
			frappe.throw(_("Memory Snapshot Disk must be at least {0} MiB.").format(self.image_size_mib))

	def validate_artifacts(self) -> None:
		"""Require both artifacts with an exact size and digest."""
		if self.is_stored_in_site_file:
			self.validate_site_file_artifacts()
		elif not self.image_object_key or not self.kernel_object_key:
			frappe.throw(_("An available Virtual Machine Image requires rootfs and kernel object keys."))
		if not SHA256_PATTERN.fullmatch(self.image_sha256 or ""):
			frappe.throw(_("Image SHA-256 must contain 64 lowercase hexadecimal characters."))
		if not SHA256_PATTERN.fullmatch(self.kernel_sha256 or ""):
			frappe.throw(_("Kernel SHA-256 must contain 64 lowercase hexadecimal characters."))
		if self.image_size_mib <= 0 or self.kernel_size_mib <= 0:
			frappe.throw(_("An available Virtual Machine Image requires positive artifact sizes."))
		self.validate_initrd_artifact()

	def validate_initrd_artifact(self) -> None:
		"""Require complete metadata when an optional initrd is present."""
		initrd_object_key = self.get("initrd_object_key")
		initrd_file = self.get("initrd_file")
		initrd_sha256 = self.get("initrd_sha256")
		initrd_size_mib = self.get("initrd_size_mib")
		if not any((initrd_object_key, initrd_file, initrd_sha256, initrd_size_mib)):
			return

		if self.is_stored_in_site_file:
			if not initrd_file:
				frappe.throw(_("An initrd stored as a site file requires an initrd file."))
		elif not initrd_object_key:
			frappe.throw(_("An initrd stored in object storage requires an initrd object key."))
		if not SHA256_PATTERN.fullmatch(initrd_sha256 or ""):
			frappe.throw(_("Initrd SHA-256 must contain 64 lowercase hexadecimal characters."))
		if not isinstance(initrd_size_mib, int) or isinstance(initrd_size_mib, bool) or initrd_size_mib <= 0:
			frappe.throw(_("An initrd requires a positive artifact size."))

	def validate_site_file_artifacts(self) -> None:
		"""Require both public files, and refuse tenant content on a public URL."""
		if self.image_type != "system":
			frappe.throw(_("Only a System image can use site file storage."))
		if not self.image_file or not self.kernel_file:
			frappe.throw(_("A site file Virtual Machine Image requires rootfs and kernel files."))

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
		if self.status != "Failed" or not self.source_server:
			frappe.throw(_("Only a failed image transfer from a virtual machine can be retried."))

		from atlas.vm.core.vm_image_transfer import VirtualMachineImageTransferService

		VirtualMachineImageTransferService().enqueue(self.name, queue="long", timeout=7200)

	@frappe.whitelist(methods=["POST"])
	def migrate_to_object_storage(self) -> None:
		"""Queue the move of this site file image into object storage."""
		self.check_permission("write")

		from atlas.vm.core.vm_image_storage_migration import VirtualMachineImageStorageMigration

		VirtualMachineImageStorageMigration().request(self)

	@frappe.whitelist(methods=["POST"])
	def request_deletion(self) -> str:
		"""Retire this image and return the status it reached."""
		self.check_permission("write")

		from atlas.vm.core.vm_image_deletion import VirtualMachineImageDeletionService

		return VirtualMachineImageDeletionService().request(self)

	@frappe.whitelist(methods=["POST"])
	def set_termination_protection(self, is_protected: bool | int | str) -> None:
		"""Set or clear termination protection."""
		self.check_permission("write")
		self.is_termination_protected = strict_bool(is_protected, "is_protected")
		self.save()

	def ensure_not_termination_protected(self) -> None:
		"""Reject removal while termination protection holds this image."""
		if self.is_termination_protected:
			frappe.throw(
				_("Virtual Machine Image {0} is termination protected.").format(self.title),
				exc=AtlasUserError,
			)
