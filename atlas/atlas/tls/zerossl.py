"""ZeroSSL TLS provider — registered stub.

ZeroSSL also speaks ACME, so the eventual implementation is close to
`LetsEncryptProvider` with a different directory URL and EAB credentials. Not
built this iteration; registered only so the `TLS Provider.provider_type` Select
option resolves and `for_tls_provider` returns a clear "not implemented" rather
than "no implementation for provider_type".
"""

from __future__ import annotations

import frappe
from frappe import _

from atlas.atlas.dns.base import DnsProvider
from atlas.atlas.tls import register
from atlas.atlas.tls.base import AuthResult, IssuedCert, TlsProvider


@register
class ZeroSslProvider(TlsProvider):
	provider_type = "ZeroSSL"

	def authenticate(self) -> AuthResult:
		return AuthResult(ok=False, error="ZeroSSL is not implemented yet")

	def issue(self, domain: str, dns_provider: DnsProvider) -> IssuedCert:
		frappe.throw(_("ZeroSSL issuance is not implemented yet"))
