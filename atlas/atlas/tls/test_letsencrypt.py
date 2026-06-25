"""Unit tests for the Let's Encrypt issuer — asserts it composes the right
issue-cert argv and credential env and parses the typed result, all WITHOUT
running certbot (the local task runner is mocked). The DNS provider is a stub so
the test stays in the TLS layer."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from atlas.atlas.dns.base import AuthResult as DnsAuthResult
from atlas.atlas.dns.base import DnsProvider, WildcardTargets
from atlas.atlas.tls import letsencrypt


class _StubDns(DnsProvider):
	provider_type = "Stub"

	def authenticate(self) -> DnsAuthResult:
		return DnsAuthResult(ok=True)

	def credential_env(self) -> dict[str, str]:
		return {"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "shh"}

	def certbot_authenticator(self) -> str:
		return "route53"

	def upsert_wildcard(self, domain: str, targets: WildcardTargets) -> list[str]:
		return []


def _provider(directory="https://acme-staging-v02.api.letsencrypt.org/directory", email="ops@example.com"):
	settings = SimpleNamespace(acme_directory_url=directory, account_email=email)
	with patch.object(letsencrypt.frappe, "get_single", return_value=settings):
		return letsencrypt.LetsEncryptProvider()


class TestLetsEncryptProvider(IntegrationTestCase):
	def test_authenticate_requires_email(self) -> None:
		self.assertTrue(_provider().authenticate().ok)
		self.assertFalse(_provider(email="").authenticate().ok)

	def test_issue_passes_certbot_args_and_credential_env(self) -> None:
		provider = _provider()
		fake_task = SimpleNamespace(
			stdout=(
				"+ certbot certonly\n"
				'ATLAS_RESULT={"fullchain_path": "/root/.atlas/certs/blr1.frappe.dev/fullchain.pem", '
				'"privkey_path": "/root/.atlas/certs/blr1.frappe.dev/privkey.pem", '
				'"not_before": "2026-06-08 00:00:00", "not_after": "2026-09-06 00:00:00"}\n'
				"Issued."
			)
		)
		with patch.object(letsencrypt, "run_local_task", return_value=fake_task) as run:
			issued = provider.issue("blr1.frappe.dev", _StubDns())

		# The DNS authenticator name reaches the script as a plain value.
		_, kwargs = run.call_args
		self.assertEqual(kwargs["script"], "issue-cert.py")
		self.assertEqual(kwargs["variables"]["DOMAIN"], "blr1.frappe.dev")
		self.assertEqual(kwargs["variables"]["DNS_AUTHENTICATOR"], "route53")
		# AWS creds travel through env, never argv.
		self.assertEqual(kwargs["env"]["AWS_ACCESS_KEY_ID"], "AKIA")

		# The typed ATLAS_RESULT line becomes an IssuedCert.
		self.assertEqual(issued.fullchain_path, "/root/.atlas/certs/blr1.frappe.dev/fullchain.pem")
		self.assertEqual(issued.privkey_path, "/root/.atlas/certs/blr1.frappe.dev/privkey.pem")
		self.assertEqual(issued.not_after, "2026-09-06 00:00:00")
