import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.central import (
	CentralClient,
	upsert_central_images,
	upsert_central_sizes,
)
from atlas.atlas.secrets import get_secret


class CentralSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Data
		api_secret: DF.Password
		atlas_id: DF.Data | None
		enabled: DF.Check
		last_event_status: DF.SmallText | None
		last_sync: DF.Datetime | None
		region: DF.Data | None
		registered_on: DF.Datetime | None
		url: DF.Data
	# end: auto-generated types

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Ping Central. Mirrors DigitalOceanSettings.test_connection — returns a
		plain dict the form turns into a toast."""
		result = self.client().ping()
		return {"ok": result.ok, "label": result.label, "error": result.error}

	@frappe.whitelist()
	def register(self) -> dict:
		"""Announce this Atlas to Central and store the returned atlas_id.

		Central owns the Atlas Instance registry: the operator pre-creates one row
		per region (carrying this Atlas's callback credentials). register() matches
		that row by region and stamps a stable atlas_id, which Atlas reports on
		every event so Central can route them back to this cluster."""
		registration = self.client().register(self._identity())
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
			frappe.throw(_("Set Central URL and API Key first"))
		secret = get_secret("Central Settings", "Central Settings", "api_secret")
		return CentralClient(self.url, self.api_key, secret)

	def _identity(self) -> dict:
		"""The registration payload Central matches against its operator-created
		Atlas Instance row. Central keys on region; base_url is sent so the operator
		can confirm the row points at this Atlas. Field names match Central's
		`central.api.register` contract."""
		region = self.region or frappe.conf.get("atlas_do_region")
		if not region:
			frappe.throw(_("Set a Region (or atlas_do_region in site config) before registering"))
		return {
			"region": region,
			"base_url": frappe.utils.get_url(),
		}
