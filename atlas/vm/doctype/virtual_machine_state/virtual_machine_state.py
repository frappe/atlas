from frappe.model.document import Document


class VirtualMachineState(Document):
	"""The last state a Metal host reported for one virtual machine."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		server: DF.Link
		status: DF.Data
		synced_at: DF.Datetime
		virtual_machine: DF.Link
	# end: auto-generated types
