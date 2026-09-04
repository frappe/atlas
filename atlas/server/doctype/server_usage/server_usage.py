from frappe.model.document import Document


class ServerUsage(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		available_cpu_count: DF.Int
		available_memory_mib: DF.Int
		available_storage_mib: DF.Int
		server: DF.Link
		total_cpu_count: DF.Int
		total_memory_mib: DF.Int
		total_storage_mib: DF.Int
		virtual_machine_count: DF.Int
	# end: auto-generated types

	"""Record one capacity sample from a Metal host."""
