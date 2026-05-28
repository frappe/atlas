"""Tests for the Provider DocType (renamed from Server Provider).

The polymorphic blob tests moved to
`providers/test_digitalocean.py`, `providers/test_self_managed.py`, and
`providers/test_worker.py`. What remains here is the controller surface:
immutability, archive, authenticate, refresh catalog, provision_server.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.provider import provider as provider_module
from atlas.atlas.providers.base import (
	AuthResult,
	Capabilities,
	ImageInfo,
	ProvisionResult,
	SizeInfo,
)
from atlas.tests.fixtures import make_provider, make_provider_row


class TestProviderRow(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.db.delete("Provider", {"provider_name": "test-imm-prov"})
		self.provider = make_provider_row(name="test-imm-prov")

	def test_provider_name_immutable(self) -> None:
		self.provider.provider_name = "renamed-provider"
		with self.assertRaises(frappe.ValidationError) as raised:
			self.provider.save(ignore_permissions=True)
		self.assertIn("provider_name is immutable", str(raised.exception))

	def test_provider_type_immutable(self) -> None:
		self.provider.reload()
		self.provider.provider_type = "Self-Managed"
		with self.assertRaises(frappe.ValidationError) as raised:
			self.provider.save(ignore_permissions=True)
		self.assertIn("provider_type is immutable", str(raised.exception))

	def test_archive_flips_is_active(self) -> None:
		self.provider.reload()
		self.provider.archive()
		self.assertEqual(
			frappe.db.get_value("Provider", self.provider.name, "is_active"),
			0,
		)

	def test_archive_throws_when_already_archived(self) -> None:
		self.provider.reload()
		self.provider.archive()
		self.provider.reload()
		with self.assertRaises(frappe.ValidationError) as raised:
			self.provider.archive()
		self.assertIn("already archived", str(raised.exception))


class TestProviderAuthenticate(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider(name="test-auth-prov")

	def test_authenticate_returns_dict(self) -> None:
		fake_impl = MagicMock()
		fake_impl.authenticate.return_value = AuthResult(
			ok=True, account_label="x@y.com", rate_limit=5000, rate_remaining=4998
		)
		with patch(
			"atlas.atlas.doctype.provider.provider.providers.for_provider",
			return_value=fake_impl,
		):
			result = self.provider.authenticate()
		self.assertTrue(result["ok"])
		self.assertEqual(result["account_label"], "x@y.com")
		self.assertEqual(result["rate_limit"], 5000)

	def test_authenticate_bad_returns_error(self) -> None:
		fake_impl = MagicMock()
		fake_impl.authenticate.return_value = AuthResult(ok=False, error="401")
		with patch(
			"atlas.atlas.doctype.provider.provider.providers.for_provider",
			return_value=fake_impl,
		):
			result = self.provider.authenticate()
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"], "401")


class TestProviderRefreshCatalog(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider(name="test-refresh-prov")
		import json
		if not frappe.db.exists("Provider Size", "DigitalOcean/legacy-slug"):
			frappe.get_doc({
				"doctype": "Provider Size",
				"provider_type": "DigitalOcean",
				"slug": "legacy-slug",
				"enabled": 1,
				"provider_metadata": json.dumps({}),
			}).insert(ignore_permissions=True)

	def tearDown(self) -> None:
		for name in ("DigitalOcean/legacy-slug", "DigitalOcean/brand-new-slug"):
			if frappe.db.exists("Provider Size", name):
				frappe.delete_doc(
					"Provider Size", name, force=True, ignore_permissions=True
				)

	def test_discover_and_upsert_counts_inserts_updates_disables(self) -> None:
		fake_impl = MagicMock()
		fake_impl.discover.return_value = Capabilities(
			sizes=(
				SizeInfo(slug="s-2vcpu-4gb-intel", monthly_cost_usd=24),
				SizeInfo(slug="brand-new-slug", monthly_cost_usd=99),
			),
			images=(ImageInfo(slug="ubuntu-24-04-x64"),),
		)
		with patch(
			"atlas.atlas.doctype.provider.provider.providers.for_provider",
			return_value=fake_impl,
		):
			result = self.provider.discover_and_upsert()
		self.assertGreaterEqual(result["inserted"], 1)
		self.assertGreaterEqual(result["updated"], 2)
		self.assertGreaterEqual(result["disabled"], 1)
		self.assertEqual(
			frappe.db.get_value("Provider Size", "DigitalOcean/legacy-slug", "enabled"),
			0,
		)


class TestProviderProvisionServer(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider(name="test-provision-prov")

	def test_provision_server_inserts_and_enqueues(self) -> None:
		title = "test-srv-1"
		frappe.db.delete("Server", {"title": title})

		fake_impl = MagicMock()
		fake_impl.provision.return_value = ProvisionResult(
			provider_resource_id="999",
			size="DigitalOcean/s-2vcpu-4gb-intel",
			image="DigitalOcean/ubuntu-24-04-x64",
			ready=False,
			networking=None,
			provider_metadata={"id": 999},
		)
		with patch(
			"atlas.atlas.doctype.provider.provider.providers.for_provider",
			return_value=fake_impl,
		), patch.object(provider_module.frappe, "enqueue") as enqueue:
			returned = self.provider.provision_server(title)

		server = frappe.get_doc("Server", returned)
		self.assertEqual(server.title, title)
		self.assertEqual(server.status, "Pending")
		self.assertEqual(server.provider_resource_id, "999")
		self.assertEqual(server.size, "DigitalOcean/s-2vcpu-4gb-intel")
		enqueue.assert_called_once()
		args, kwargs = enqueue.call_args
		self.assertEqual(args[0], "atlas.atlas.providers.worker.finish_provisioning")
		self.assertEqual(kwargs["server_name"], returned)
		frappe.db.delete("Server", {"title": title})

	def test_provision_server_rejects_duplicate(self) -> None:
		title = "dup-server"
		frappe.db.delete("Server", {"title": title})
		frappe.get_doc({
			"doctype": "Server",
			"title": title,
			"provider": self.provider.name,
			"provider_resource_id": "1",
			"status": "Pending",
		}).insert(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError) as raised:
			self.provider.provision_server(title)
		self.assertIn("already exists", str(raised.exception))
		frappe.db.delete("Server", {"title": title})


class TestProviderProvisionServerSelfManaged(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.db.delete("Provider", {"provider_name": "test-self-managed-row"})
		self.provider = make_provider_row(
			name="test-self-managed-row", provider_type="Self-Managed"
		)
		from atlas.tests.fixtures import set_atlas_settings
		set_atlas_settings(self.provider)

	def test_provision_server_self_managed_inserts(self) -> None:
		title = "self-managed-srv-1"
		frappe.db.delete("Server", {"title": title})

		with patch.object(provider_module.frappe, "enqueue") as enqueue:
			returned = self.provider.provision_server(
				title,
				ipv4_address="203.0.113.10",
				ipv6_address="2001:db8::1",
				ipv6_prefix="2001:db8::/64",
				ipv6_virtual_machine_range="2001:db8:dead::/64",
			)

		server = frappe.get_doc("Server", returned)
		self.assertEqual(server.title, title)
		self.assertEqual(server.status, "Pending")
		self.assertEqual(server.ipv4_address, "203.0.113.10")
		self.assertEqual(server.ipv6_address, "2001:db8::1")
		self.assertFalse(server.provider_resource_id)
		enqueue.assert_called_once()
		frappe.db.delete("Server", {"title": title})

	def test_provision_server_self_managed_requires_addresses(self) -> None:
		title = "self-managed-missing"
		frappe.db.delete("Server", {"title": title})
		with self.assertRaises(frappe.ValidationError) as raised:
			self.provider.provision_server(title)
		self.assertIn("ipv4_address", str(raised.exception))
		self.assertFalse(frappe.db.exists("Server", {"title": title}))
