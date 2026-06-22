import frappe
from frappe import _
from frappe.model.document import Document


class ProviderSize(Document):
	def autoname(self) -> None:
		if not self.provider_type or not self.slug:
			frappe.throw(_("Provider Size requires provider_type and slug"))
		self.name = f"{self.provider_type}/{self.slug}"

	def validate(self) -> None:
		expected = f"{self.provider_type}/{self.slug}"
		if self.name and self.name != expected:
			frappe.throw(f"Provider Size name {self.name!r} does not match {expected!r}")
