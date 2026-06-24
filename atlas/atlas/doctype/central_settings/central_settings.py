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
		hub_endpoint: DF.Data | None
		hub_public_key: DF.Data | None
		last_event_status: DF.SmallText | None
		last_sync: DF.Datetime | None
		region: DF.Data | None
		registered_on: DF.Datetime | None
		tunnel_cidr: DF.Data | None
		tunnel_ip: DF.Data | None
		tunnel_status: DF.Literal["Inactive", "Provisioning", "Active", "Reverting"]
		url: DF.Data
		wg_listen_port: DF.Int
		wg_public_key: DF.Data | None
	# end: auto-generated types

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Ping Central. Mirrors DigitalOceanSettings.test_connection — returns a
		plain dict the form turns into a toast."""
		result = self.client().ping()
		return {"ok": result.ok, "label": result.label, "error": result.error}

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
