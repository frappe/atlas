"""Route53 Settings — AWS Route 53 credentials + the active DNS vendor type.

The secret is read via `atlas.atlas.secrets.get_secret` by `Route53DnsProvider`.
`domain_provider_type` is the active DNS vendor (the DNS registry keys off it);
`test_connection` is the Test Connection button the deleted `Domain Provider`
form used to own.
"""

from __future__ import annotations

import dataclasses

import frappe
from frappe.model.document import Document


class Route53Settings(Document):
	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Test Connection button — Route 53 ListHostedZones via the DNS provider."""
		from atlas.atlas import dns

		result = dns.for_dns_provider_type(self.domain_provider_type or "Route53").authenticate()
		return dataclasses.asdict(result)
