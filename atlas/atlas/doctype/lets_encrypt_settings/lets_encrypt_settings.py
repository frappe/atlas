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

# Let's Encrypt staging — no rate limits, untrusted cert. The default keeps an
# unattended setup from burning LE production issuance quota; pass the production
# directory URL for a trusted cert. Mirrors bootstrap.LETS_ENCRYPT_STAGING.
LETS_ENCRYPT_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"


class LetsEncryptSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		account_email: DF.Data
		acme_directory_url: DF.Data
	# end: auto-generated types

	@frappe.whitelist()
	def setup(self, account_email: str, acme_directory_url: str = LETS_ENCRYPT_STAGING) -> None:
		"""Explicit, idempotent setter for Lets Encrypt Settings (the contract).

		`acme_directory_url` defaults to LE STAGING so an unattended setup never burns
		production issuance quota. Writes via `set_single_value` (NOT `doc.save()`) so
		it stays re-runnable. (There is no `agree_tos` field on this Single — ToS
		acceptance is implicit in the ACME account registration the provider drives.)"""
		frappe.db.set_single_value(
			"Lets Encrypt Settings", "account_email", account_email, update_modified=False
		)
		frappe.db.set_single_value(
			"Lets Encrypt Settings",
			"acme_directory_url",
			acme_directory_url or LETS_ENCRYPT_STAGING,
			update_modified=False,
		)

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Test Connection button — Let's Encrypt account check via the TLS provider."""
		from atlas.atlas import tls

		result = tls.for_tls_provider_type("Let's Encrypt").authenticate()
		return dataclasses.asdict(result)
