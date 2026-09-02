# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class ServerDisk(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		device: DF.Data
		mount_point: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		size_gb: DF.Data | None
		uuid: DF.Data | None
	# end: auto-generated types

	pass
