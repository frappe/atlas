from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from atlas.atlas.core.dns_providers.base import DnsProviderError
from atlas.atlas.core.dns_providers.static import StaticProvider


class TestStaticProvider(TestCase):
	def setUp(self) -> None:
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.addCleanup(patch.stopall)
		patch.dict(frappe.conf, developer_mode=1).start()
		patch("frappe.get_site_path", return_value=str(Path(self.directory.name) / "dns.sqlite3")).start()
		self.settings = SimpleNamespace(wildcard_domain="metal.test")
		self.provider = StaticProvider(self.settings)

	def test_persists_addresses_and_cname_and_replaces_record_type(self) -> None:
		self.provider.upsert_record("A", "cache.metal.test", ["192.0.2.1", "192.0.2.2"])
		self.provider.upsert_cname_record("alias.metal.test", "cache.metal.test")
		self.assertEqual(
			StaticProvider(self.settings).resolve("alias.metal.test"), ["192.0.2.1", "192.0.2.2"]
		)
		self.provider.upsert_record("AAAA", "cache.metal.test", ["fdaa:1::1"], replaced_type="A")
		self.assertEqual(self.provider.resolve("alias.metal.test"), ["fdaa:1::1"])
		self.assertIn("host-record=alias.metal.test,fdaa:1::1", self.provider.render_dnsmasq())

	def test_multivalue_updates_and_removes_only_its_identifier(self) -> None:
		for identifier, address in [("one", "192.0.2.1"), ("two", "192.0.2.2")]:
			self.provider.upsert_multivalue_a_record("proxy.metal.test", identifier, address, "ignored")
		self.provider.upsert_multivalue_a_record("proxy.metal.test", "one", "192.0.2.3", "ignored")
		self.provider.remove_multivalue_a_record("proxy.metal.test", "two")
		self.assertEqual(self.provider.resolve("proxy.metal.test"), ["192.0.2.3"])

	def test_wildcard_cname_and_exact_mesh_override(self) -> None:
		self.provider.upsert_a_record("proxy.metal.test", "192.0.2.1")
		self.provider.upsert_cname_record("*.metal.test", "proxy.metal.test")
		self.provider.upsert_record("AAAA", "internal-runway-cache-svc.metal.test", ["fdaa:1::1"])
		self.assertEqual(self.provider.resolve("runway-cache-svc.metal.test"), ["192.0.2.1"])
		self.assertEqual(self.provider.resolve("internal-runway-cache-svc.metal.test"), ["fdaa:1::1"])
		configuration = self.provider.render_dnsmasq()
		self.assertIn("address=/metal.test/192.0.2.1", configuration)
		self.assertIn("host-record=internal-runway-cache-svc.metal.test,fdaa:1::1", configuration)
		self.assertIn("listen-address=127.0.0.1", configuration)

	def test_rejects_cycles_and_rolls_back_type_replacement(self) -> None:
		self.provider.upsert_cname_record("one.metal.test", "two.metal.test")
		self.provider.upsert_a_record("two.metal.test", "192.0.2.1")
		with self.assertRaises(DnsProviderError):
			self.provider.upsert_record("CNAME", "two.metal.test", ["one.metal.test"], replaced_type="A")
		self.assertEqual(self.provider.resolve("one.metal.test"), ["192.0.2.1"])

	def test_rejects_public_domains_and_non_developer_sites(self) -> None:
		with self.assertRaises(DnsProviderError):
			StaticProvider(SimpleNamespace(wildcard_domain="example.com"))
		with patch.dict(frappe.conf, developer_mode=0), self.assertRaises(DnsProviderError):
			StaticProvider(self.settings)

	def test_rejects_invalid_values_and_outside_names(self) -> None:
		for record_type, name, values in [
			("A", "cache.metal.test", ["::1"]),
			("AAAA", "cache.metal.test", ["not-an-address"]),
			("TXT", "cache.metal.test", ["value"]),
			("A", "cache.example.com", ["192.0.2.1"]),
			("A", "bad\nname.metal.test", ["192.0.2.1"]),
			("CNAME", "cache.metal.test", ["example.com"]),
		]:
			with self.subTest(record_type=record_type, name=name), self.assertRaises(DnsProviderError):
				self.provider.upsert_record(record_type, name, values)

	def test_cname_address_conflict_and_idempotent_delete(self) -> None:
		self.provider.upsert_cname_record("alias.metal.test", "proxy.metal.test")
		with self.assertRaises(DnsProviderError):
			self.provider.upsert_a_record("alias.metal.test", "192.0.2.1")
		self.provider.remove_cname_record("alias.metal.test")
		self.provider.remove_cname_record("alias.metal.test")
		self.assertEqual(self.provider.resolve("alias.metal.test"), [])
