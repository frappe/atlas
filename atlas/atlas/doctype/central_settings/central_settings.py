import json

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.central import CentralClient
from atlas.atlas.secrets import get_secret


class CentralSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Data
		api_secret: DF.Password
		enabled: DF.Check
		hub_endpoint: DF.Data | None
		hub_public_key: DF.Data | None
		status: DF.SmallText | None
		tunnel_cidr: DF.Data | None
		tunnel_ip: DF.Data | None
		tunnel_status: DF.Literal["Inactive", "Provisioning", "Active", "Reverting"]
		url: DF.Data
		version_image_map: DF.JSON | None
		wg_listen_port: DF.Int
		wg_public_key: DF.Data | None
	# end: auto-generated types

	def onload(self) -> None:
		"""Compute `version_image_map` for the form on open — the versions Central can
		offer and the active admin image each resolves to. Live from this region's active
		admin images, never stored: the field is read-only and this is the only writer, so
		it always reflects what Central pulls from `available_frappe_versions`."""
		from atlas.atlas.placement import version_image_map

		self.version_image_map = json.dumps(version_image_map(), indent=2)

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Ping Central. Mirrors DigitalOceanSettings.test_connection — returns a
		plain dict the form turns into a toast."""
		result = self.client().ping()
		return {"ok": result.ok, "label": result.label, "error": result.error}

	def client(self) -> CentralClient:
		if not self.url or not self.api_key:
			frappe.throw(_("Set Central URL and API Key first"))
		secret = get_secret("Central Settings", "Central Settings", "api_secret")
		return CentralClient(self.url, self.api_key, secret)
