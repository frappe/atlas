"""Self-Managed TLS provider — operator drops PEMs at the configured paths.

The escape hatch for when Atlas should not run an ACME client: the operator
issues the wildcard cert out of band and places `fullchain.pem` / `privkey.pem`
at the cert paths themselves. `issue()` therefore acquires nothing — it asserts
the PEMs are already on disk and reads their validity window. Mirrors the compute
`SelfManagedProvider`, which echoes operator-supplied truth rather than calling a
vendor.
"""

from __future__ import annotations

import frappe
from frappe import _

from atlas.atlas.dns.base import DnsProvider
from atlas.atlas.tls import register
from atlas.atlas.tls.base import AuthResult, IssuedCert, TlsProvider


@register
class SelfManagedTlsProvider(TlsProvider):
	provider_type = "Self-Managed"

	def authenticate(self) -> AuthResult:
		return AuthResult(ok=True, account_label="self-managed")

	def issue(self, domain: str, dns_provider: DnsProvider) -> IssuedCert:
		frappe.throw(
			_(
				"Self-Managed TLS does not issue certificates. Place fullchain.pem and "
				"privkey.pem at the TLS Certificate's paths, then use Push to Proxies."
			)
		)
