import dataclasses

import frappe
from frappe.model.document import Document


class DigitalOceanSettings(Document):
	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Ping DigitalOcean using the DigitalOcean provider's authenticate()."""
		from atlas.atlas import providers

		result = providers.for_provider_type("DigitalOcean").authenticate()
		return dataclasses.asdict(result)
