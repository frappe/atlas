# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MetalServerDrainMigration(Document):
	"""One migration in a Metal Server Drain, with a copy of its status and progress."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		destination_metal_server: DF.Link | None
		error_message: DF.SmallText | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		progress_percent: DF.Int
		status: DF.Literal[
			"scheduled",
			"preparing",
			"copying",
			"cutting_over",
			"starting",
			"finalizing",
			"canceling",
			"completed",
			"failed",
			"aborted",
		]
		virtual_machine: DF.Link | None
		virtual_machine_migration: DF.Link | None
	# end: auto-generated types

	pass
