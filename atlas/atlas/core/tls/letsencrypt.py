from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import frappe
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from atlas.atlas.core.dns_providers import DnsRecord
from atlas.atlas.core.tls.acme import AcmeClient, DnsChallenge
from atlas.atlas.core.tls.certificate import (
	create_certificate_request,
	create_private_key,
	read_certificate,
	serialize_private_key,
)

if TYPE_CHECKING:
	from atlas.atlas.core.dns_providers.base import DnsProvider
	from atlas.atlas.doctype.atlas_settings.atlas_settings import AtlasSettings

DIRECTORY_URLS = {
	"Production": "https://acme-v02.api.letsencrypt.org/directory",
	"Staging": "https://acme-staging-v02.api.letsencrypt.org/directory",
}


class LetsEncryptError(Exception):
	"""Raised when Atlas cannot issue a certificate from Let's Encrypt."""


@dataclass(frozen=True)
class IssuedCertificate:
	"""One issued certificate chain, its private key, and its UTC expiry."""

	certificate_pem: str
	private_key_pem: str
	expires_on: datetime


class LetsEncrypt:
	"""Issue the Atlas wildcard certificate with the ACME dns-01 challenge."""

	DNS_PROPAGATION_DELAY = 30

	def __init__(self, settings: "AtlasSettings | None" = None) -> None:
		self.settings: AtlasSettings = settings or frappe.get_single("Atlas Settings")

	@property
	def domain(self) -> str:
		"""Return the zone the wildcard belongs to."""
		return self.settings.wildcard_domain.removeprefix("*.")

	@property
	def wildcard_domain(self) -> str:
		"""Return the identifier the certificate is issued for."""
		return f"*.{self.domain}"

	@property
	def environment(self) -> str:
		"""Return the ACME environment the settings select."""
		return "Staging" if self.settings.is_letsencrypt_staging else "Production"

	@property
	def directory_url(self) -> str:
		"""Return the ACME directory URL of the selected environment."""
		return DIRECTORY_URLS[self.environment]

	@property
	def config_directory(self) -> Path:
		"""Return the account-key directory."""
		configured = (self.settings.letsencrypt_config_directory or "").strip()
		return Path(configured) if configured else Path(frappe.get_site_path("letsencrypt"))

	@property
	def account_key_path(self) -> Path:
		"""Return the account-key path for the selected environment."""
		return self.config_directory / self.environment.lower() / "account.key"

	def get_account_key(self) -> ec.EllipticCurvePrivateKey:
		"""Return the ACME account key, and create it on first use."""
		path = self.account_key_path
		if path.exists():
			return self._read_account_key(path)

		path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
		private_key = ec.generate_private_key(ec.SECP256R1())
		path.write_bytes(
			private_key.private_bytes(
				encoding=serialization.Encoding.PEM,
				format=serialization.PrivateFormat.PKCS8,
				encryption_algorithm=serialization.NoEncryption(),
			)
		)
		path.chmod(0o600)
		return private_key

	def issue_wildcard_certificate(self) -> IssuedCertificate:
		"""Issue a wildcard certificate through DNS."""
		if not self.settings.letsencrypt_email:
			raise LetsEncryptError("Set the Let's Encrypt email in Atlas Settings before issuing.")

		client = AcmeClient(self.directory_url, self.get_account_key())
		client.register_account(self.settings.letsencrypt_email)

		order = client.create_order([self.wildcard_domain])
		challenges = [client.get_dns_challenge(url) for url in order.authorization_urls]
		try:
			self._prove_control(client, order.authorization_urls, challenges)
			private_key = create_private_key()
			request = create_certificate_request(private_key, [self.wildcard_domain])
			certificate_url = client.finalize_order(order, request)
			certificate_pem = client.download_certificate(certificate_url)
		finally:
			self._remove_challenge_records(challenges)

		return IssuedCertificate(
			certificate_pem=certificate_pem,
			private_key_pem=serialize_private_key(private_key),
			expires_on=read_certificate(certificate_pem).expires_on,
		)

	@property
	def _dns_provider(self) -> "DnsProvider":
		return self.settings.dns_provider_controller

	def _prove_control(
		self, client: AcmeClient, authorization_urls: list[str], challenges: list[DnsChallenge]
	) -> None:
		"""Publish every TXT value, then let the server check each authorization."""
		for record_name, values in self._group_values(challenges).items():
			self._dns_provider.upsert_records([DnsRecord(record_name, "TXT", values, 60)])

		time.sleep(self.DNS_PROPAGATION_DELAY)

		for challenge in challenges:
			client.submit_challenge(challenge.challenge_url)
		for authorization_url in authorization_urls:
			client.wait_for_authorization(authorization_url)

	def _remove_challenge_records(self, challenges: list[DnsChallenge]) -> None:
		"""Remove the challenge records. A failure here must not hide an issuance failure."""
		for record_name in self._group_values(challenges):
			try:
				self._dns_provider.remove_records(self._dns_provider.get_records(record_name, "TXT"))
			except Exception:
				frappe.log_error(title=f"Unable to remove ACME record {record_name}")

	@staticmethod
	def _group_values(challenges: list[DnsChallenge]) -> dict[str, list[str]]:
		"""Group values by record name. One name holds the values of every identifier under it."""
		grouped: dict[str, list[str]] = defaultdict(list)
		for challenge in challenges:
			grouped[challenge.record_name].append(challenge.record_value)
		return dict(grouped)

	@staticmethod
	def _read_account_key(path: Path) -> ec.EllipticCurvePrivateKey:
		private_key = serialization.load_pem_private_key(path.read_bytes(), password=None)
		if not isinstance(private_key, ec.EllipticCurvePrivateKey):
			raise LetsEncryptError(f"The ACME account key at {path} is not an EC key.")
		return private_key
