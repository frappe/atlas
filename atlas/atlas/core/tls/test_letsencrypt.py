from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives.asymmetric import ec
from frappe.tests import UnitTestCase

from atlas.atlas.core.dns_providers import DnsRecord
from atlas.atlas.core.tls.acme import DnsChallenge, Order
from atlas.atlas.core.tls.certificate import create_private_key
from atlas.atlas.core.tls.letsencrypt import (
	DIRECTORY_URLS,
	IssuedCertificate,
	LetsEncrypt,
	LetsEncryptError,
)
from atlas.atlas.core.tls.test_certificate import self_signed_certificate


class _FakeSettings:
	def __init__(self, directory: str, is_staging: int = 0) -> None:
		self.wildcard_domain = "example.com"
		self.letsencrypt_email = "owner@example.com"
		self.letsencrypt_config_directory = directory
		self.is_letsencrypt_staging = is_staging
		self.dns_provider_controller = MagicMock()


class _LetsEncryptCase(UnitTestCase):
	def setUp(self) -> None:
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.issuer = LetsEncrypt(settings=_FakeSettings(self.directory.name))
		self.issuer.DNS_PROPAGATION_DELAY = 0

	@property
	def dns_provider(self) -> MagicMock:
		return self.issuer.settings.dns_provider_controller


class TestLetsEncryptAccount(_LetsEncryptCase):
	def test_the_account_key_is_created_once_and_kept_private(self) -> None:
		created = self.issuer.get_account_key()
		reloaded = self.issuer.get_account_key()

		path = self.issuer.account_key_path
		self.assertEqual(path, Path(self.directory.name) / "production" / "account.key")
		self.assertEqual(path.stat().st_mode & 0o777, 0o600)
		self.assertEqual(created.private_numbers().private_value, reloaded.private_numbers().private_value)

	def test_each_environment_keeps_its_own_account_and_directory(self) -> None:
		staging = LetsEncrypt(settings=_FakeSettings(self.directory.name, is_staging=1))

		self.assertEqual(staging.environment, "Staging")
		self.assertEqual(staging.directory_url, DIRECTORY_URLS["Staging"])
		self.assertNotEqual(staging.account_key_path, self.issuer.account_key_path)

	def test_a_key_that_is_not_an_ec_key_is_refused(self) -> None:
		path = self.issuer.account_key_path
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text("not a key")

		with self.assertRaises(ValueError):
			self.issuer.get_account_key()

	def test_a_blank_config_directory_uses_the_site_path(self) -> None:
		self.issuer.settings.letsencrypt_config_directory = ""

		with patch("atlas.atlas.core.tls.letsencrypt.frappe.get_site_path") as get_site_path:
			get_site_path.return_value = "sites/test.local/letsencrypt"

			self.assertEqual(self.issuer.config_directory, Path("sites/test.local/letsencrypt"))
			get_site_path.assert_called_once_with("letsencrypt")


class TestLetsEncryptIssuance(_LetsEncryptCase):
	def setUp(self) -> None:
		super().setUp()
		self.client = MagicMock()
		self.client.create_order.return_value = Order(
			url="https://acme.test/order/1",
			authorization_urls=["https://acme.test/authz/1"],
			finalize_url="https://acme.test/order/1/finalize",
		)
		self.client.get_dns_challenge.return_value = DnsChallenge(
			challenge_url="https://acme.test/challenge/1",
			record_name="_acme-challenge.example.com",
			record_value="challenge-value",
		)
		self.client.finalize_order.return_value = "https://acme.test/cert/1"
		patched = patch("atlas.atlas.core.tls.letsencrypt.AcmeClient", return_value=self.client)
		self.addCleanup(patched.stop)
		self.acme_client = patched.start()

	def _issue(self) -> IssuedCertificate:
		self.client.download_certificate.return_value = self_signed_certificate(
			create_private_key(), ["*.example.com"]
		)
		return self.issuer.issue_wildcard_certificate()

	def test_issuance_proves_control_through_dns_and_returns_the_certificate(self) -> None:
		issued = self._issue()

		directory_url, account_key = self.acme_client.call_args.args
		self.assertEqual(directory_url, DIRECTORY_URLS["Production"])
		self.assertIsInstance(account_key, ec.EllipticCurvePrivateKey)
		self.client.register_account.assert_called_once_with("owner@example.com")
		self.client.create_order.assert_called_once_with(["*.example.com"])
		self.dns_provider.upsert_records.assert_called_once_with(
			[DnsRecord("_acme-challenge.example.com", "TXT", ["challenge-value"], 60)]
		)
		self.client.submit_challenge.assert_called_once_with("https://acme.test/challenge/1")
		self.client.wait_for_authorization.assert_called_once_with("https://acme.test/authz/1")
		self.assertIn("BEGIN CERTIFICATE", issued.certificate_pem)
		self.assertIn("BEGIN PRIVATE KEY", issued.private_key_pem)
		self.assertIsNotNone(issued.expires_on)

	def test_the_challenge_record_is_removed_after_a_successful_issuance(self) -> None:
		self._issue()

		self.dns_provider.get_records.assert_called_once_with("_acme-challenge.example.com", "TXT")

	def test_the_challenge_record_is_removed_after_a_failed_order(self) -> None:
		self.client.finalize_order.side_effect = RuntimeError("order failed")

		with self.assertRaises(RuntimeError):
			self.issuer.issue_wildcard_certificate()

		self.dns_provider.get_records.assert_called_once_with("_acme-challenge.example.com", "TXT")

	def test_issuance_without_an_email_is_refused(self) -> None:
		self.issuer.settings.letsencrypt_email = ""

		with self.assertRaises(LetsEncryptError):
			self.issuer.issue_wildcard_certificate()

		self.client.create_order.assert_not_called()
