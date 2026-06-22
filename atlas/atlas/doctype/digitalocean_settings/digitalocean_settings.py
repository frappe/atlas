import dataclasses

import frappe
from frappe import _
from frappe.model.document import Document


class DigitalOceanSettings(Document):
	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Ping DigitalOcean using the active Provider's authenticate()."""
		from atlas.atlas import providers

		provider_name = frappe.db.get_single_value("Atlas Settings", "provider")
		if not provider_name:
			frappe.throw(_("Set Atlas Settings.provider before testing the connection"))
		result = providers.for_provider(provider_name).authenticate()
		return dataclasses.asdict(result)
