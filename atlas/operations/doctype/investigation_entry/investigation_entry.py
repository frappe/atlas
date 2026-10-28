# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

MAXIMUM_TITLE_LENGTH = 140


class InvestigationEntry(Document):
	"""One tool call, decision, action, or note in an Investigation log."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		detail: DF.Code | None
		is_approved: DF.Check
		kind: DF.Literal["Tool Call", "Action", "Decision", "Note"]
		occurred_at: DF.Datetime
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		reference_doctype: DF.Data | None
		reference_name: DF.Data | None
		status: DF.Literal["", "Success", "Failed", "Refused"]
		title: DF.Data
		user: DF.Data | None
	# end: auto-generated types

	@staticmethod
	def append_to(investigation: str, **values: object) -> None:
		"""Insert one row in the log of an Investigation.

		Each row is its own insert. Saving the Investigation would rewrite its table and lose a row
		that a parallel tool call inserts.
		"""
		row = frappe.get_doc(
			{
				"doctype": "Investigation Entry",
				"parenttype": "Investigation",
				"parentfield": "entries",
				"parent": investigation,
				"idx": frappe.db.count("Investigation Entry", {"parent": investigation}) + 1,
				"occurred_at": now_datetime(),
				**values,
			}
		)
		row.title = str(row.title)[:MAXIMUM_TITLE_LENGTH]
		row.db_insert()
