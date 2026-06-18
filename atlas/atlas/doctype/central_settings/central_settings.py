import frappe
from frappe.model.document import Document

from atlas.atlas.central import (
	CentralClient,
	upsert_central_images,
	upsert_central_sizes,
)
from atlas.atlas.secrets import get_secret


class CentralSettings(Document):
	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Ping Central. Mirrors DigitalOceanSettings.test_connection — returns a
		plain dict the form turns into a toast."""
		result = self.client().ping()
		return {"ok": result.ok, "label": result.label, "error": result.error}

	@frappe.whitelist()
	def register(self) -> dict:
		"""Announce this Atlas to Central and store the returned atlas_id."""
		registration = self.client().register(self._identity_payload())
		self.atlas_id = registration.atlas_id
		self.registered_on = frappe.utils.now_datetime()
		self.save()
		return {"ok": True, "atlas_id": registration.atlas_id, "label": registration.label}

	@frappe.whitelist()
	def fetch_sizes(self) -> dict:
		"""Pull Central's VM size catalog into Central Size rows."""
		summary = upsert_central_sizes(self.client().fetch_sizes())
		self.db_set("last_sync", frappe.utils.now_datetime())
		return summary

	@frappe.whitelist()
	def fetch_images(self) -> dict:
		"""Pull Central's expected bench images into Central Image rows."""
		summary = upsert_central_images(self.client().fetch_images())
		self.db_set("last_sync", frappe.utils.now_datetime())
		return summary

	def client(self) -> CentralClient:
		if not self.url or not self.api_key:
			frappe.throw("Set Central URL and API Key first")
		secret = get_secret("Central Settings", "Central Settings", "api_secret")
		return CentralClient(self.url, self.api_key, secret)

	def _identity_payload(self) -> dict:
		provider = frappe.db.get_single_value("Atlas Settings", "provider")
		return {
			"region": self.region or frappe.conf.get("atlas_do_region"),
			"provider": provider,
			"provider_type": frappe.db.get_value("Provider", provider, "provider_type") if provider else None,
			"site": frappe.local.site,
		}
