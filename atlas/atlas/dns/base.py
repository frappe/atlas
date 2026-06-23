"""DNS provider abstraction — the DNS-01 half of certificate issuance, plus the
wildcard record that points the regional domain at its proxy fleet.

A `DnsProvider` knows how to prove control of a zone to an ACME server via the
DNS-01 challenge. For the challenge Atlas never writes TXT records itself; it hands
certbot the provider's plugin flag (`certbot_authenticator()`) and the vendor
credentials as env (`credential_env()`), and certbot's DNS plugin does the record
dance. Atlas *does* write the public `*.<domain>` A/AAAA records itself
(`upsert_wildcard()`), so a client resolving `<sub>.<domain>` reaches the proxy
fleet — that record is the durable routing entry, not a transient challenge. The
seam mirrors the compute `Provider` ABC (`atlas/atlas/providers/base.py`): callers
ask `for_dns_provider_type(type)` for an instance and never branch on `provider_type`.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from typing import ClassVar


@dataclasses.dataclass(frozen=True, slots=True)
class AuthResult:
	"""Outcome of a credential check — twin of the compute `AuthResult`, trimmed
	to what a DNS account exposes."""

	ok: bool
	account_label: str | None = None
	error: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class WildcardTargets:
	"""The proxy fleet's public addresses the regional wildcard should resolve to:
	`ipv4` (the reserved IPs attached to the proxies) and `ipv6` (the proxies'
	`/128`s). DNS round-robins over each list (spec/12-proxy.md)."""

	ipv4: list[str]
	ipv6: list[str]


class DnsProvider(ABC):
	provider_type: ClassVar[str]

	@abstractmethod
	def authenticate(self) -> AuthResult:
		"""Verify the credentials can reach the zone (Route 53: GetHostedZone).
		Backs the Domain Provider's **Test Connection** button."""
		...

	@abstractmethod
	def upsert_wildcard(self, domain: str, targets: WildcardTargets) -> list[str]:
		"""Publish `*.<domain>` A → `targets.ipv4` and AAAA → `targets.ipv6`,
		round-robin over the proxy fleet. Idempotent UPSERT: the record is replaced
		with exactly these targets each call, so a rebuilt proxy (new `/128`) or a
		reattached reserved IP is reflected on the next reconcile. An empty family is
		skipped (and any stale record of that type left as-is — we never publish a
		wildcard pointing at nothing). Returns the record names written."""
		...

	@abstractmethod
	def credential_env(self) -> dict[str, str]:
		"""Vendor secrets as the environment certbot's DNS plugin reads (Route 53:
		`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`). Merged into the issue-cert
		subprocess env, never placed in argv (secrets must not show up in `ps`)."""
		...

	@abstractmethod
	def certbot_authenticator(self) -> str:
		"""The certbot DNS authenticator NAME for this vendor (Route 53: `route53`).
		The issue-cert script turns it into the plugin flag (`--dns-route53`); the
		name (never a `--`-prefixed token) is what crosses the typed-CLI boundary,
		so argparse can't mistake a value for an option. No credentials here — those
		go through `credential_env()`."""
		...
