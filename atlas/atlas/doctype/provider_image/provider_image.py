import frappe
from frappe import _
from frappe.model.document import Document


class ProviderImage(Document):
	def autoname(self) -> None:
		if not self.provider_type or not self.slug:
			frappe.throw(_("Provider Image requires provider_type and slug"))
		self.name = f"{self.provider_type}/{self.slug}"

	def validate(self) -> None:
		expected = f"{self.provider_type}/{self.slug}"
		if self.name and self.name != expected:
			frappe.throw(f"Provider Image name {self.name!r} does not match {expected!r}")
