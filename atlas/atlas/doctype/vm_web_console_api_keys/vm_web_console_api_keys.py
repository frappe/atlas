# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime
import frappe

class VMWebConsoleAPIKeys(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		creation_time: DF.Datetime | None
		expiry_time: DF.Datetime | None
		virtual_machine: DF.Link | None
	# end: auto-generated types

	_DOCTYPE_NAME = "VM Web Console API Keys"

@frappe.whitelist(allow_guest=True)
def get_console_session(name: str):
	doc = frappe.db.get_value(
		"VM Web Console API Keys",
		name,
		[
			"expiry_time",
			"virtual_machine",
		],
		as_dict=True,
	)

	if not doc:
		frappe.throw("Invalid console session")

	if (
		doc.expiry_time
		and doc.expiry_time < now_datetime()
	):
		frappe.throw("Console session has expired")

	return doc.virtual_machine
