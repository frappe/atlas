"""PowerDNS Settings — Authoritative HTTP API credentials."""

from __future__ import annotations

import dataclasses

import frappe
from frappe.model.document import Document


class PowerDNSSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Password
		api_url: DF.Data
		server_id: DF.Data | None
	# end: auto-generated types

	@frappe.whitelist()
	def setup(self, api_url: str, api_key: str, server_id: str = "localhost") -> None:
		self.api_url = api_url
		self.api_key = api_key
		self.server_id = server_id or "localhost"
		self.save(ignore_permissions=True)

	@frappe.whitelist()
	def test_connection(self) -> dict:
		from atlas.atlas import dns

		result = dns.for_dns_provider_type("PowerDNS").authenticate()
		return dataclasses.asdict(result)
