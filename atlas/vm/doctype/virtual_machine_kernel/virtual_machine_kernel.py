from __future__ import annotations

from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.core.artifacts import get_download_url
from atlas.atlas.core.exceptions import AtlasConflictError

if TYPE_CHECKING:
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings


class VirtualMachineKernel(Document):
	"""One kernel file, stored once and shared by every image that boots it."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		architecture: DF.Literal["amd64", "arm64"]
		file: DF.Link | None
		object_key: DF.Data | None
		sha256: DF.Data
		size_mib: DF.Int
	# end: auto-generated types

	def on_trash(self) -> None:
		"""Remove the stored kernel once no image boots from it."""
		if frappe.db.exists("Virtual Machine Image", {"kernel": self.name}):
			frappe.throw(_("A Virtual Machine Image still uses this kernel."), exc=AtlasConflictError)
		if self.object_key:
			settings: AtlasSettings = frappe.get_single("Atlas Settings")
			settings.get_object_storage_client().delete_object(self.object_key)
		if self.file:
			frappe.delete_doc("File", self.file, force=True, ignore_permissions=True, delete_permanently=True)

	def get_url(self, expiry_seconds: int) -> str:
		"""Return the download URL. A kernel in object storage no longer uses its site file."""
		if self.object_key:
			settings: AtlasSettings = frappe.get_single("Atlas Settings")
			return settings.get_object_storage_client().object_url(
				self.object_key, expiry_seconds=expiry_seconds
			)
		return get_download_url(self.file)
