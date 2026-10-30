from frappe.model.document import Document


class MetalServerDrainVirtualMachine(Document):
	"""One Virtual Machine selected for a Metal Server Drain."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		virtual_machine: DF.Link
	# end: auto-generated types
