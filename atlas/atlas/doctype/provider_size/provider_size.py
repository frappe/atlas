import frappe
from frappe import _
from frappe.model.document import Document


class ProviderSize(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		enabled: DF.Check
		monthly_cost_usd: DF.Int
		provider_metadata: DF.Code | None
		provider_type: DF.Literal["DigitalOcean", "Scaleway", "Self-Managed", "Fake"]
		slug: DF.Data
	# end: auto-generated types

	def autoname(self) -> None:
		if not self.provider_type or not self.slug:
			frappe.throw(_("Provider Size requires provider_type and slug"))
		self.name = f"{self.provider_type}/{self.slug}"

	def validate(self) -> None:
		expected = f"{self.provider_type}/{self.slug}"
		if self.name and self.name != expected:
			frappe.throw(f"Provider Size name {self.name!r} does not match {expected!r}")
