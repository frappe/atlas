"""Let's Encrypt TLS provider — ACME DNS-01 via certbot, run on the controller.

Reads `Let's Encrypt Settings` (ACME directory, account email, ToS agreement) and
issues `*.<domain>` by driving the `scripts/issue-cert.py` Task locally
(`atlas.atlas.local_task.run_local_task`). The certbot DNS authenticator and its
credentials come from the *DNS* provider — `certbot_args()` into the script argv,
`credential_env()` into the subprocess env — so the issuer is agnostic to which
DNS vendor proves control. The issued PEMs land on the controller's disk; the
script emits their paths and the validity window as the typed `ATLAS_RESULT=`
line, which we parse back into an `IssuedCert`.
"""

from __future__ import annotations

import frappe
from frappe import _

from atlas.atlas.dns.base import DnsProvider
from atlas.atlas.local_task import run_local_task
from atlas.atlas.task_results import parse_result
from atlas.atlas.tls import register
from atlas.atlas.tls.base import AuthResult, IssuedCert, TlsProvider

LETS_ENCRYPT_PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"


@register
class LetsEncryptProvider(TlsProvider):
	provider_type = "Let's Encrypt"

	def __init__(self) -> None:
		settings = frappe.get_single("Lets Encrypt Settings")
		self.acme_directory_url = settings.acme_directory_url or LETS_ENCRYPT_PRODUCTION
		self.account_email = settings.account_email

	def authenticate(self) -> AuthResult:
		if not self.account_email:
			return AuthResult(ok=False, error="Lets Encrypt Settings has no account_email")
		return AuthResult(ok=True, account_label=self.account_email)

	def issue(self, domain: str, dns_provider: DnsProvider) -> IssuedCert:
		# certbot is invoked with --agree-tos (scripts/lib/atlas/certs.py): registering
		# the ACME account agrees to the ToS, so there is no separate gate to check.
		if not self.account_email:
			frappe.throw(_("Lets Encrypt Settings: account_email is required"))

		variables = {
			"DOMAIN": domain,
			"ACME_DIRECTORY_URL": self.acme_directory_url,
			"ACCOUNT_EMAIL": self.account_email,
			# The DNS authenticator name (e.g. route53); the script renders the
			# certbot flag (--dns-route53). A plain name, never a --flag, so it
			# crosses the typed-CLI boundary without confusing argparse.
			"DNS_AUTHENTICATOR": dns_provider.certbot_authenticator(),
		}
		task = run_local_task(
			script="issue-cert.py",
			variables=variables,
			env=dns_provider.credential_env(),
			timeout_seconds=600,
		)
		result = parse_result(task.stdout)
		return IssuedCert(
			fullchain_path=result["fullchain_path"],
			privkey_path=result["privkey_path"],
			not_before=result["not_before"],
			not_after=result["not_after"],
		)
