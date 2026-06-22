"""Domain Provider DocType — thin link table over the DNS provider abstraction.

Twin of `Provider` (compute): stores only `provider_name` / `provider_type` /
`is_active` and delegates `authenticate` to the registered `DnsProvider`
implementation ([atlas.atlas.dns](../../dns/)).
"""

from __future__ import annotations

import dataclasses

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas import dns

IMMUTABLE_AFTER_INSERT = ("provider_name", "provider_type")


class DomainProvider(Document):
	def validate(self) -> None:
		self._validate_immutability()

	def _validate_immutability(self) -> None:
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			if getattr(self, field) != getattr(original, field):
				frappe.throw(f"{field} is immutable after insert")

	@frappe.whitelist()
	def archive(self) -> None:
		"""Flip is_active=0. Existing Root Domain FKs survive."""
		if not self.is_active:
			frappe.throw(_("Domain Provider is already archived"))
		frappe.db.set_value(self.doctype, self.name, "is_active", 0)

	@frappe.whitelist()
	def authenticate(self) -> dict:
		"""Test Connection button — Route 53 GetHostedZone via the DNS provider."""
		result = dns.for_domain_provider(self.name).authenticate()
		return dataclasses.asdict(result)
