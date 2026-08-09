import frappe
from frappe import _
from frappe.model.document import Document


class ProviderImage(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		enabled: DF.Check
		is_default: DF.Check
		provider_metadata: DF.Code | None
		provider_type: DF.Literal["DigitalOcean", "Scaleway", "Self-Managed", "Fake"]
		slug: DF.Data
	# end: auto-generated types

	def autoname(self) -> None:
		if not self.provider_type or not self.slug:
			frappe.throw(_("Provider Image requires provider_type and slug"))
		self.name = f"{self.provider_type}/{self.slug}"

	def validate(self) -> None:
		expected = f"{self.provider_type}/{self.slug}"
		if self.name and self.name != expected:
			frappe.throw(f"Provider Image name {self.name!r} does not match {expected!r}")
		if self.is_default:
			from atlas.atlas.setup_catalog import clear_other_defaults

			clear_other_defaults("Provider Image", self.provider_type, self.name)
