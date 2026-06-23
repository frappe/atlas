import dataclasses

import frappe
from frappe.model.document import Document


class ScalewaySettings(Document):
	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Ping Scaleway using the Scaleway provider's authenticate()."""
		from atlas.atlas import providers

		result = providers.for_provider_type("Scaleway").authenticate()
		return dataclasses.asdict(result)
