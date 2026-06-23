"""Unit tests for the DNS provider registry — twin of
`atlas/atlas/providers/test_registry.py`. The registry maps `provider_type` →
implementation class directly (no `Domain Provider` row to load)."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import dns
from atlas.atlas.dns.base import AuthResult, DnsProvider, WildcardTargets


class _StubDnsProvider(DnsProvider):
	provider_type = "Stub"

	def authenticate(self) -> AuthResult:
		return AuthResult(ok=True, account_label="stub")

	def credential_env(self) -> dict[str, str]:
		return {"STUB": "1"}

	def certbot_authenticator(self) -> str:
		return "stub"

	def upsert_wildcard(self, domain: str, targets: WildcardTargets) -> list[str]:
		return [f"A *.{domain}"]


class TestDnsProviderRegistry(IntegrationTestCase):
	def setUp(self) -> None:
		dns._REGISTRY["Stub"] = _StubDnsProvider

	def tearDown(self) -> None:
		dns._REGISTRY.pop("Stub", None)

	def test_register_decorator_stores_class(self) -> None:
		@dns.register
		class _DecoratorStub(DnsProvider):
			provider_type = "DecoratorStub"

			def authenticate(self) -> AuthResult:
				return AuthResult(ok=True)

			def credential_env(self) -> dict[str, str]:
				return {}

			def certbot_authenticator(self) -> str:
				return "decorator-stub"

			def upsert_wildcard(self, domain: str, targets: WildcardTargets) -> list[str]:
				return []

		try:
			self.assertIs(dns._REGISTRY["DecoratorStub"], _DecoratorStub)
		finally:
			dns._REGISTRY.pop("DecoratorStub", None)

	def test_for_dns_provider_type_instantiates_registered_class(self) -> None:
		with patch.object(dns, "_load_implementations", lambda: None):
			instance = dns.for_dns_provider_type("Stub")
		self.assertIsInstance(instance, _StubDnsProvider)

	def test_for_dns_provider_type_throws_on_unknown_type(self) -> None:
		with patch.object(dns, "_load_implementations", lambda: None):
			with self.assertRaises(frappe.ValidationError) as raised:
				dns.for_dns_provider_type("Unregistered")
		self.assertIn("No implementation", str(raised.exception))
