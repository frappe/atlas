import dataclasses

import frappe
from frappe.model.document import Document


class DigitalOceanSettings(Document):
	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Ping DigitalOcean using the active Provider's authenticate()."""
		from atlas.atlas import providers

		provider_name = frappe.db.get_single_value("Atlas Settings", "provider")
		if not provider_name:
			frappe.throw("Set Atlas Settings.provider before testing the connection")
		result = providers.for_provider(provider_name).authenticate()
		return dataclasses.asdict(result)

	@frappe.whitelist()
	def credential_check(self) -> dict:
		"""Auto-called on form refresh by the client script to paint the
		green/red indicator. Returns the same payload shape on success and
		failure so the client only looks at `ok`."""
		from atlas.atlas import providers

		provider_name = frappe.db.get_single_value("Atlas Settings", "provider")
		if not provider_name:
			return {"ok": False, "error": "Atlas Settings.provider is not set"}
		result = providers.for_provider(provider_name).authenticate()
		return dataclasses.asdict(result)
