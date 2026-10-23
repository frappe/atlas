from frappe.model.document import Document


class VirtualMachineState(Document):
	"""The last state a Metal host reported for one virtual machine."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		last_request_at: DF.Datetime | None
		server: DF.Link
		sleep_snapshot_id: DF.Data | None
		sleep_state: DF.Literal["", "sleeping", "deep-sleeping", "coma"]
		sleep_urls_expire_at: DF.Datetime | None
		sleep_urls_snapshot_id: DF.Data | None
		status: DF.Data
		synced_at: DF.Datetime
		virtual_machine: DF.Link
	# end: auto-generated types
