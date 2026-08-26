# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class GarageVirtualMachines(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		peer_id: DF.Data | None
		virtual_machine: DF.Link
	# end: auto-generated types

	_DOCTYPE_NAME = "Garage Virtual Machines"
