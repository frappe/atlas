# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class MetalServerDrain(Document):
	"""Move every Virtual Machine off one Metal Server."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from atlas.operations.doctype.metal_server_drain_destination.metal_server_drain_destination import (
			MetalServerDrainDestination,
		)
		from atlas.operations.doctype.metal_server_drain_migration.metal_server_drain_migration import (
			MetalServerDrainMigration,
		)

		allow_any_destination: DF.Check
		allow_disk_only_fallback: DF.Check
		amended_from: DF.Link | None
		destinations: DF.Table[MetalServerDrainDestination]
		duration_seconds: DF.Duration | None
		finished_at: DF.Datetime | None
		has_memory_snapshot: DF.Check
		migrations: DF.Table[MetalServerDrainMigration]
		progress_percent: DF.Int
		source_metal_server: DF.Link
		started_at: DF.Datetime | None
		status: DF.Literal["Draft", "Running", "Completed", "Failed", "Cancelled"]
	# end: auto-generated types

	@property
	def allowed_destinations(self) -> set[str] | None:
		"""Return the Metal Servers that placement may choose, or None for any of them."""
		if self.allow_any_destination:
			return None
		return {row.metal_server for row in self.destinations}

	def validate(self) -> None:
		destinations = [row.metal_server for row in self.destinations]
		if not self.allow_any_destination and not destinations:
			frappe.throw(_("Add a destination or allow any destination."))
		if self.source_metal_server in destinations:
			frappe.throw(_("The Source Metal Server cannot be a destination."))
		if len(destinations) != len(set(destinations)):
			frappe.throw(_("Each destination can be in the drain only once."))
