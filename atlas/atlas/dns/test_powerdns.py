"""Unit tests for the PowerDNS DNS provider."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from atlas.atlas.dns import powerdns
from atlas.atlas.dns.base import WildcardTargets


def _provider(api_url="https://pdns.example.test", api_key="secret", server_id="localhost"):
	settings = SimpleNamespace(api_url=api_url, server_id=server_id)
	with (
		patch.object(powerdns.frappe, "get_single", return_value=settings),
		patch.object(powerdns, "get_secret", return_value=api_key),
	):
		return powerdns.PowerDNSDnsProvider()


class TestPowerDNSDnsProvider(IntegrationTestCase):
	def test_certbot_authenticator_is_powerdns(self) -> None:
		self.assertEqual(_provider().certbot_authenticator(), "powerdns")

	def test_certbot_args_use_powerdns_credentials_file(self) -> None:
		args = _provider().certbot_args("blr1.frappe.dev")
		self.assertEqual(args[args.index("--authenticator") + 1], "dns-pdns")
		path = args[args.index("--dns-pdns-credentials") + 1]
		self.assertTrue(os.path.isabs(path))
		self.assertTrue(path.endswith("/.atlas/certbot/blr1.frappe.dev/powerdns.ini"))

	def test_credential_env_carries_powerdns_settings(self) -> None:
		env = _provider(
			api_url="https://pdns.example.test/", api_key="shh", server_id="primary"
		).credential_env()
		self.assertEqual(env["POWERDNS_API_URL"], "https://pdns.example.test")
		self.assertEqual(env["POWERDNS_API_KEY"], "shh")
		self.assertEqual(env["POWERDNS_SERVER_ID"], "primary")

	def test_authenticate_reads_server_endpoint(self) -> None:
		provider = _provider(server_id="primary")
		with patch.object(
			provider, "_request", return_value={"id": "primary", "version": "4.9.0"}
		) as request:
			result = provider.authenticate()
		self.assertTrue(result.ok)
		self.assertEqual(result.account_label, "primary (4.9.0)")
		request.assert_called_once_with("GET", "/servers/primary")

	def test_upsert_wildcard_replaces_a_and_aaaa(self) -> None:
		provider = _provider(server_id="primary")
		calls = []

		def fake_request(method, path, **kwargs):
			calls.append((method, path, kwargs))
			if method == "GET" and kwargs.get("params") == {"zone": "atlas1.x.frappe.dev."}:
				return []
			if method == "GET" and kwargs.get("params") == {"zone": "x.frappe.dev."}:
				return [{"id": "x.frappe.dev."}]
			return {}

		with patch.object(provider, "_request", side_effect=fake_request):
			records = provider.upsert_wildcard(
				"atlas1.x.frappe.dev",
				WildcardTargets(ipv4=["1.2.3.4"], ipv6=["2400:abcd::1"]),
			)
		self.assertEqual(records, ["A *.atlas1.x.frappe.dev", "AAAA *.atlas1.x.frappe.dev"])
		method, path, kwargs = calls[-1]
		self.assertEqual(method, "PATCH")
		self.assertEqual(path, "/servers/primary/zones/x.frappe.dev.")
		rrsets = kwargs["json"]["rrsets"]
		by_type = {rrset["type"]: rrset for rrset in rrsets}
		self.assertEqual(by_type["A"]["name"], "*.atlas1.x.frappe.dev.")
		self.assertEqual(by_type["A"]["changetype"], "REPLACE")
		self.assertEqual(by_type["A"]["records"], [{"content": "1.2.3.4", "disabled": False}])
		self.assertEqual(by_type["AAAA"]["records"], [{"content": "2400:abcd::1", "disabled": False}])

	def test_upsert_wildcard_skips_empty_family(self) -> None:
		provider = _provider()
		with (
			patch.object(provider, "_zone_id", return_value="x.frappe.dev."),
			patch.object(provider, "_request", return_value={}) as request,
		):
			records = provider.upsert_wildcard(
				"atlas1.x.frappe.dev", WildcardTargets(ipv4=["1.2.3.4"], ipv6=[])
			)
		self.assertEqual(records, ["A *.atlas1.x.frappe.dev"])
		rrsets = request.call_args.kwargs["json"]["rrsets"]
		self.assertEqual([rrset["type"] for rrset in rrsets], ["A"])

	def test_upsert_wildcard_throws_when_no_targets(self) -> None:
		import frappe

		provider = _provider()
		with self.assertRaises(frappe.ValidationError):
			provider.upsert_wildcard("atlas1.x.frappe.dev", WildcardTargets(ipv4=[], ipv6=[]))

	def test_zone_id_throws_when_no_zone_matches(self) -> None:
		import frappe

		provider = _provider()
		with patch.object(provider, "_request", return_value=[]):
			with self.assertRaises(frappe.ValidationError):
				provider._zone_id("atlas1.x.frappe.dev")
