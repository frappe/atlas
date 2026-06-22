"""Tests for the Provider DocType (renamed from Server Provider).

The polymorphic blob tests moved to
`providers/test_digitalocean.py`, `providers/test_self_managed.py`, and
`providers/test_worker.py`. What remains here is the controller surface:
immutability, archive, authenticate, refresh catalog, provision_server.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.provider import provider as provider_module
from atlas.atlas.providers.base import (
	AuthResult,
	Capabilities,
	DiscoveredServer,
	ImageInfo,
	ProvisionResult,
	ServerNetworking,
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
			frappe.get_doc(
				{
					"doctype": "Provider Size",
					"provider_type": "DigitalOcean",
					"slug": "legacy-slug",
					"enabled": 1,
					"provider_metadata": json.dumps({}),
				}
			).insert(ignore_permissions=True)

	def tearDown(self) -> None:
		for name in ("DigitalOcean/legacy-slug", "DigitalOcean/brand-new-slug"):
			if frappe.db.exists("Provider Size", name):
				frappe.delete_doc("Provider Size", name, force=True, ignore_permissions=True)

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
		with (
			patch(
				"atlas.atlas.doctype.provider.provider.providers.for_provider",
				return_value=fake_impl,
			),
			patch.object(provider_module.frappe, "enqueue") as enqueue,
		):
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
		frappe.get_doc(
			{
				"doctype": "Server",
				"title": title,
				"provider": self.provider.name,
				"provider_resource_id": "1",
				"status": "Pending",
			}
		).insert(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError) as raised:
			self.provider.provision_server(title)
		self.assertIn("already exists", str(raised.exception))
		frappe.db.delete("Server", {"title": title})


class TestProviderProvisionServerSelfManaged(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.db.delete("Provider", {"provider_name": "test-self-managed-row"})
		self.provider = make_provider_row(name="test-self-managed-row", provider_type="Self-Managed")
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


class TestProviderDiscoverServers(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider(name="test-discover-prov")
		frappe.db.delete("Server", {"provider": self.provider.name})

	def _list_servers(self):
		return (
			DiscoveredServer(
				provider_resource_id="srv-modeled",
				title="already-here",
				ipv4_address="51.159.1.1",
				size="DigitalOcean/s-2vcpu-4gb",
			),
			DiscoveredServer(
				provider_resource_id="srv-new",
				title="adopt-me",
				ipv4_address="51.159.2.2",
				size="DigitalOcean/s-4vcpu-8gb",
			),
		)

	def test_discover_flags_already_modeled(self) -> None:
		# A Server already models srv-modeled.
		frappe.get_doc(
			{
				"doctype": "Server",
				"title": "already-here",
				"provider": self.provider.name,
				"provider_resource_id": "srv-modeled",
				"status": "Active",
			}
		).insert(ignore_permissions=True)

		fake_impl = MagicMock()
		fake_impl.list_servers.return_value = self._list_servers()
		with patch(
			"atlas.atlas.doctype.provider.provider.providers.for_provider",
			return_value=fake_impl,
		):
			result = self.provider.discover_servers()

		by_id = {row["provider_resource_id"]: row for row in result}
		self.assertTrue(by_id["srv-modeled"]["imported"])
		self.assertFalse(by_id["srv-new"]["imported"])
		# Preview fields surface for the picker.
		self.assertEqual(by_id["srv-new"]["title"], "adopt-me")
		self.assertEqual(by_id["srv-new"]["ipv4_address"], "51.159.2.2")
		self.assertEqual(by_id["srv-new"]["size"], "DigitalOcean/s-4vcpu-8gb")
		frappe.db.delete("Server", {"provider": self.provider.name})

	def test_discover_inserts_nothing(self) -> None:
		fake_impl = MagicMock()
		fake_impl.list_servers.return_value = self._list_servers()
		before = frappe.db.count("Server", {"provider": self.provider.name})
		with patch(
			"atlas.atlas.doctype.provider.provider.providers.for_provider",
			return_value=fake_impl,
		):
			self.provider.discover_servers()
		after = frappe.db.count("Server", {"provider": self.provider.name})
		self.assertEqual(before, after)


class TestProviderImportServers(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider(name="test-import-prov")
		frappe.db.delete("Server", {"provider": self.provider.name})

	def _ready_result(self, resource_id: str) -> ProvisionResult:
		return ProvisionResult(
			provider_resource_id=resource_id,
			size="DigitalOcean/s-2vcpu-4gb",
			image="DigitalOcean/ubuntu-24-04-x64",
			ready=True,
			networking=ServerNetworking(
				ipv4_address="51.159.9.9",
				ipv6_address="2a03:b0c0:1::1",
				ipv6_prefix="2a03:b0c0:1::/64",
				ipv6_virtual_machine_range="2a03:b0c0:1::/124",
			),
			provider_metadata={"id": resource_id},
		)

	def test_import_inserts_pending_row_from_describe(self) -> None:
		fake_impl = MagicMock()
		fake_impl.describe.return_value = self._ready_result("srv-import-1")
		# The picker's discovery carries the friendly hostname; import titles the
		# row with it (describe() has no clean hostname field).
		fake_impl.list_servers.return_value = (
			DiscoveredServer(provider_resource_id="srv-import-1", title="my-scaleway-box"),
		)
		with patch(
			"atlas.atlas.doctype.provider.provider.providers.for_provider",
			return_value=fake_impl,
		):
			# Dialog posts resource_ids as a JSON string.
			result = self.provider.import_servers(json.dumps(["srv-import-1"]))

		self.assertEqual(len(result["imported"]), 1)
		self.assertEqual(result["skipped"], [])
		server = frappe.get_doc("Server", result["imported"][0]["name"])
		self.assertEqual(server.status, "Pending")
		self.assertEqual(server.provider_resource_id, "srv-import-1")
		# Title is the vendor hostname from discovery, not the UUID.
		self.assertEqual(server.title, "my-scaleway-box")
		# describe() is the authority for the fields — networking/size/image filled.
		self.assertEqual(server.ipv4_address, "51.159.9.9")
		self.assertEqual(server.ipv6_virtual_machine_range, "2a03:b0c0:1::/124")
		self.assertEqual(server.size, "DigitalOcean/s-2vcpu-4gb")
		# Authoritative describe() was called for the picked id.
		fake_impl.describe.assert_called_once_with("srv-import-1")
		frappe.db.delete("Server", {"provider": self.provider.name})

	def test_import_falls_back_to_resource_id_when_no_hostname(self) -> None:
		"""A vendor box with no hostname (list_servers title=None) titles the row
		with the resource id."""
		fake_impl = MagicMock()
		fake_impl.describe.return_value = self._ready_result("srv-noname")
		fake_impl.list_servers.return_value = (
			DiscoveredServer(provider_resource_id="srv-noname", title=None),
		)
		with patch(
			"atlas.atlas.doctype.provider.provider.providers.for_provider",
			return_value=fake_impl,
		):
			result = self.provider.import_servers(json.dumps(["srv-noname"]))
		server = frappe.get_doc("Server", result["imported"][0]["name"])
		self.assertEqual(server.title, "srv-noname")
		frappe.db.delete("Server", {"provider": self.provider.name})

	def test_import_skips_already_modeled(self) -> None:
		frappe.get_doc(
			{
				"doctype": "Server",
				"title": "existing-box",
				"provider": self.provider.name,
				"provider_resource_id": "srv-existing",
				"status": "Active",
			}
		).insert(ignore_permissions=True)

		fake_impl = MagicMock()
		fake_impl.list_servers.return_value = ()
		with patch(
			"atlas.atlas.doctype.provider.provider.providers.for_provider",
			return_value=fake_impl,
		):
			result = self.provider.import_servers(json.dumps(["srv-existing"]))

		self.assertEqual(result["imported"], [])
		self.assertEqual(result["skipped"], ["srv-existing"])
		# Never re-described or re-inserted.
		fake_impl.describe.assert_not_called()
		self.assertEqual(frappe.db.count("Server", {"provider_resource_id": "srv-existing"}), 1)
		frappe.db.delete("Server", {"provider": self.provider.name})

	def test_import_is_idempotent_across_runs(self) -> None:
		fake_impl = MagicMock()
		fake_impl.describe.return_value = self._ready_result("srv-twice")
		fake_impl.list_servers.return_value = (
			DiscoveredServer(provider_resource_id="srv-twice", title="twice-box"),
		)
		with patch(
			"atlas.atlas.doctype.provider.provider.providers.for_provider",
			return_value=fake_impl,
		):
			first = self.provider.import_servers(json.dumps(["srv-twice"]))
			second = self.provider.import_servers(json.dumps(["srv-twice"]))

		self.assertEqual(len(first["imported"]), 1)
		self.assertEqual(second["imported"], [])
		self.assertEqual(second["skipped"], ["srv-twice"])
		self.assertEqual(frappe.db.count("Server", {"provider_resource_id": "srv-twice"}), 1)
		frappe.db.delete("Server", {"provider": self.provider.name})

	def test_import_dedups_title_collision(self) -> None:
		"""A discovered hostname collides with an existing Server.title → the import
		gets a -2 suffix so the unique-title guard doesn't reject it."""
		fake_impl = MagicMock()
		fake_impl.describe.side_effect = lambda rid: ProvisionResult(
			provider_resource_id=rid,
			size="",
			image="",
			ready=False,
			networking=None,
			provider_metadata=None,
		)
		# The vendor box's hostname is "web-1"; an existing Server already uses it.
		fake_impl.list_servers.return_value = (
			DiscoveredServer(provider_resource_id="srv-dupe", title="web-1"),
		)
		frappe.get_doc(
			{
				"doctype": "Server",
				"title": "web-1",
				"provider": self.provider.name,
				"provider_resource_id": "some-other-id",
				"status": "Active",
			}
		).insert(ignore_permissions=True)
		with patch(
			"atlas.atlas.doctype.provider.provider.providers.for_provider",
			return_value=fake_impl,
		):
			result = self.provider.import_servers(json.dumps(["srv-dupe"]))
		self.assertEqual(len(result["imported"]), 1)
		self.assertEqual(result["imported"][0]["title"], "web-1-2")
		frappe.db.delete("Server", {"provider": self.provider.name})


class TestProviderDiscoverImportDeskShape(IntegrationTestCase):
	"""Drive Discover Servers + Import through the HTTP wrapper the desk hits
	(`run_doc_method`), with the exact arg shapes the dialog posts: no args for
	`discover_servers`, and `resource_ids` as a JSON *string* for
	`import_servers`. The plan calls this the desk-button trap — a direct Python
	call passes a typed list and never exercises the string-decode path."""

	def setUp(self) -> None:
		self.provider = make_provider(name="test-desk-discover-prov")
		frappe.db.delete("Server", {"provider": self.provider.name})

	def _call(self, method: str, **kwargs):
		from frappe.handler import run_doc_method

		frappe.response.pop("message", None)
		frappe.response.docs = []
		previous = getattr(frappe.local, "request", None)
		frappe.local.request = SimpleNamespace(method="POST")
		try:
			run_doc_method(
				method=method,
				dt="Provider",
				dn=self.provider.name,
				args=json.dumps(kwargs),
			)
		finally:
			if previous is None:
				try:
					del frappe.local.request
				except AttributeError:
					pass
			else:
				frappe.local.request = previous
		return frappe.response.get("message")

	def test_discover_then_import_through_run_doc_method(self) -> None:
		fake_impl = MagicMock()
		fake_impl.list_servers.return_value = (
			DiscoveredServer(
				provider_resource_id="srv-desk-1",
				title="desk-box",
				ipv4_address="51.159.7.7",
				size="DigitalOcean/s-2vcpu-4gb",
			),
		)
		fake_impl.describe.return_value = ProvisionResult(
			provider_resource_id="srv-desk-1",
			size="DigitalOcean/s-2vcpu-4gb",
			image="DigitalOcean/ubuntu-24-04-x64",
			ready=True,
			networking=ServerNetworking(
				ipv4_address="51.159.7.7",
				ipv6_address="2a03:b0c0:2::1",
				ipv6_prefix="2a03:b0c0:2::/64",
				ipv6_virtual_machine_range="2a03:b0c0:2::/124",
			),
			provider_metadata={"id": "srv-desk-1"},
		)
		with patch(
			"atlas.atlas.doctype.provider.provider.providers.for_provider",
			return_value=fake_impl,
		):
			discovered = self._call("discover_servers")
			self.assertEqual(len(discovered), 1)
			self.assertFalse(discovered[0]["imported"])

			# The dialog posts resource_ids as a JSON string — drive that branch.
			imported = self._call("import_servers", resource_ids=json.dumps(["srv-desk-1"]))

		self.assertEqual(len(imported["imported"]), 1)
		server = frappe.get_doc("Server", imported["imported"][0]["name"])
		self.assertEqual(server.status, "Pending")
		self.assertEqual(server.provider_resource_id, "srv-desk-1")
		frappe.db.delete("Server", {"provider": self.provider.name})
