"""TLS issuer abstraction — produces the wildcard cert the proxy consumes.

A `TlsProvider` turns "(wildcard) domain + a DNS provider that can answer DNS-01"
into PEMs on the controller's disk. It mirrors the compute `Provider` ABC: callers
ask `for_tls_provider_type(type)` for an instance and never branch on `provider_type`.
Let's Encrypt is the only implementation this iteration; ZeroSSL / Self-Managed
are registered stubs so the Select options resolve.

The `issue()` result is paths (not bytes) plus the cert's validity window — the
`TLS Certificate` controller records the paths and pushes the PEMs to the proxy
fleet. Private-key bytes stay on disk, out of the DB (mirroring
`Atlas Settings.ssh_private_key_path`).
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from typing import ClassVar

from atlas.atlas.dns.base import DnsProvider


@dataclasses.dataclass(frozen=True, slots=True)
class IssuedCert:
	"""What an issue/renew produced: on-disk PEM paths and the validity window
	parsed from the issued cert. `not_before`/`not_after` are ISO-8601 strings the
	controller writes straight into the `TLS Certificate` Datetime fields."""

	fullchain_path: str
	privkey_path: str
	not_before: str
	not_after: str


@dataclasses.dataclass(frozen=True, slots=True)
class AuthResult:
	ok: bool
	account_label: str | None = None
	error: str | None = None


class TlsProvider(ABC):
	provider_type: ClassVar[str]

	@abstractmethod
	def authenticate(self) -> AuthResult:
		"""Verify the issuer account is usable (ACME directory reachable / ToS
		agreed). Backs a Test Connection affordance; cheap, no issuance."""
		...

	@abstractmethod
	def issue(self, domain: str, dns_provider: DnsProvider) -> IssuedCert:
		"""Issue (or renew, idempotently) `*.<domain>`, proving control via
		`dns_provider`'s DNS-01 challenge. Returns the on-disk PEM paths and the
		validity window. Runs on the controller."""
		...
