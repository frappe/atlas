"""TLS Provider DocType — thin link table over the TLS issuer abstraction.

Twin of `Provider` (compute) and `Domain Provider` (DNS): stores only
`provider_name` / `provider_type` / `is_active` and delegates `authenticate` to
the registered `TlsProvider` implementation ([atlas.atlas.tls](../../tls/)).
"""

from __future__ import annotations

import dataclasses

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas import tls

IMMUTABLE_AFTER_INSERT = ("provider_name", "provider_type")


class TLSProvider(Document):
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
			frappe.throw(_("TLS Provider is already archived"))
		frappe.db.set_value(self.doctype, self.name, "is_active", 0)

	@frappe.whitelist()
	def authenticate(self) -> dict:
		"""Test Connection button — issuer account check via the TLS provider."""
		result = tls.for_tls_provider(self.name).authenticate()
		return dataclasses.asdict(result)
