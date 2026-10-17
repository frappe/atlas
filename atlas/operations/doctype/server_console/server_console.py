# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ServerConsole(Document):
	"""Desk form that starts a Server Console Log. It does not save."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		script: DF.Code | None
		servers: DF.SmallText | None
		timeout_seconds: DF.Int
	# end: auto-generated types

	@frappe.whitelist(methods=["POST"])
	def execute(self) -> str:
		"""Start the script on the servers and return the Server Console Log name."""
		frappe.only_for("System Manager")
		log = frappe.get_doc(
			{
				"doctype": "Server Console Log",
				"servers": self.servers,
				"script": self.script,
				"timeout_seconds": self.timeout_seconds,
			}
		)
		return log.insert(ignore_permissions=True).name
