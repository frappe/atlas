# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class ServerSize(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cpu_count: DF.Int
		disk_gib: DF.Int
		enabled: DF.Check
		hourly_pricing_usd_cents: DF.Int
		memory_mb: DF.Int
		monthly_pricing_usd_cents: DF.Int
		provider_metadata: DF.Code | None
		provider_type: DF.Literal["Scaleway"]
		size: DF.Data
	# end: auto-generated types

	def autoname(self) -> None:
		if not self.provider_type or not self.size:
			frappe.throw(_("Server Size requires provider_type and size"))
		self.name = f"{self.provider_type}/{self.size}"

	def validate(self) -> None:
		expected = f"{self.provider_type}/{self.size}"
		if self.name and self.name != expected:
			frappe.throw(_("Server Size name {0} does not match {1}").format(self.name, expected))
