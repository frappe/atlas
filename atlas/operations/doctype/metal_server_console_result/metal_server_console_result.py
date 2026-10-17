# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MetalServerConsoleResult(Document):
	"""One server in a Metal Server Console Log and its script result."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		exit_code: DF.Int
		output: DF.Code | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		server: DF.Link
		ssh_host: DF.Data | None
		status: DF.Literal["Pending", "Running", "Success", "Failed"]
	# end: auto-generated types

	pass
