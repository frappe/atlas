# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MetaldUpdateServer(Document):
	"""One server in a Metald Update and the result of its upgrade."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		batch: DF.Int
		firecracker_version: DF.Data | None
		metald_version: DF.Data | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		server: DF.Link
		ssh_task: DF.Link | None
		status: DF.Literal[
			"Pending", "Planning", "Upgrading", "Upgraded", "Checking", "Verified", "Failed", "Skipped"
		]
		status_reason: DF.SmallText | None
		wg_mesh_version: DF.Data | None
	# end: auto-generated types

	pass
