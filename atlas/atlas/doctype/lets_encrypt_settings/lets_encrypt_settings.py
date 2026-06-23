"""Lets Encrypt Settings — ACME account config for the Let's Encrypt issuer.

Storage read by `LetsEncryptProvider` (ACME directory, account email, ToS
agreement). The DocType name drops the apostrophe in "Let's Encrypt" so its
scrubbed module path is a legal Python identifier (`lets_encrypt_settings`); the
provider's Select value keeps the apostrophe since that is data, not a module.
`test_connection` is the Test Connection button the deleted `TLS Provider` form
used to own.
"""

from __future__ import annotations

import dataclasses

import frappe
from frappe.model.document import Document


class LetsEncryptSettings(Document):
	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Test Connection button — Let's Encrypt account check via the TLS provider."""
		from atlas.atlas import tls

		result = tls.for_tls_provider_type("Let's Encrypt").authenticate()
		return dataclasses.asdict(result)
