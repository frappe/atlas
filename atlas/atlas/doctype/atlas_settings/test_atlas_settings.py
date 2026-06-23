"""Tests for the Atlas Settings Single — its accessor module and the provider
buttons that relocated here from the deleted `Provider` DocType.

The polymorphic vendor behavior is covered in `providers/test_digitalocean.py`,
`providers/test_self_managed.py`, and `providers/test_worker.py`. What lives here is
the controller surface: the SSH accessors, the provider_type switch guard, and the
Authenticate / Refresh Catalog / Provision Server / Discover / Import buttons.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import atlas_settings
from atlas.atlas.providers.base import (
	AuthResult,
	Capabilities,
	DiscoveredServer,
	ImageInfo,
	ProvisionResult,
	SizeInfo,
)
from atlas.tests.fixtures import (
	_ensure_fake_ssh_key_path,
	make_provider,
	make_provider_row,
	make_server,
	set_atlas_settings,
)


class TestAtlasSettingsAccessors(IntegrationTestCase):
	def test_get_ssh_key_returns_dataclass(self) -> None:
		from atlas.atlas.providers.base import SshKey

		provider = make_provider_row(name="test-ssh-prov")
		set_atlas_settings(
			provider,
			ssh_key_id="key-id-test",
			ssh_public_key="ssh-ed25519 AAAA",
			ssh_private_key_path=_ensure_fake_ssh_key_path(),
		)
		key = atlas_settings.get_ssh_key()
		self.assertIsInstance(key, SshKey)
		self.assertEqual(key.vendor_id, "key-id-test")
		self.assertEqual(key.public_key, "ssh-ed25519 AAAA")

	def test_get_ssh_private_key_path_returns_path(self) -> None:
		provider = make_provider_row(name="test-pk-prov")
		set_atlas_settings(provider, ssh_private_key_path=_ensure_fake_ssh_key_path())
		path = atlas_settings.get_ssh_private_key_path()
		self.assertEqual(path, _ensure_fake_ssh_key_path())

	def test_get_provider_throws_when_unset(self) -> None:
		previous = frappe.db.get_single_value("Atlas Settings", "provider_type")
		try:
			frappe.db.set_single_value("Atlas Settings", "provider_type", "", update_modified=False)
			with self.assertRaises(frappe.ValidationError) as raised:
				atlas_settings.get_provider()
			self.assertIn("no provider_type", str(raised.exception))
		finally:
			if previous:
				frappe.db.set_single_value("Atlas Settings", "provider_type", previous, update_modified=False)


class TestProviderSwitchGuard(IntegrationTestCase):
	"""Switching provider_type is refused while a non-Archived Server was
	provisioned through a different vendor — the Single-world equivalent of the old
	"archive doesn't destroy Servers" promise.

	The guard reads live Servers from the (shared) test DB, so these tests exercise
	`_validate_provider_switch` directly with a controlled before-image rather than a
	full save, to stay independent of servers other test classes leave behind."""

	def _settings_switching_to(self, new_type: str):
		"""An Atlas Settings doc whose provider_type is being changed to `new_type`,
		with a before-image of the opposite vendor so the guard runs."""
		settings = frappe.get_single("Atlas Settings")
		before = frappe.copy_doc(settings)
		before.provider_type = "DigitalOcean" if new_type != "DigitalOcean" else "Scaleway"
		settings.provider_type = new_type
		settings.get_doc_before_save = lambda: before
		return settings

	def test_switch_blocked_by_live_other_vendor_server(self) -> None:
		make_server(title="switch-guard-scw", status="Active", provider_type="Scaleway")
		settings = self._settings_switching_to("DigitalOcean")
		with self.assertRaises(frappe.ValidationError) as raised:
			settings._validate_provider_switch()
		self.assertIn("provider_type", str(raised.exception))
		frappe.db.delete("Server", {"title": "switch-guard-scw"})

	def test_switch_allowed_when_no_live_other_vendor_server(self) -> None:
		# The guard only counts non-Archived Servers of a different vendor. With the
		# stranded query returning nothing (every other-vendor Server archived), the
		# switch is allowed. The shared test DB always carries live Servers of several
		# vendors, so stub the guard's query to the archived-excluded result it would
		# return on a clean fleet.
		from unittest.mock import patch

		settings = self._settings_switching_to("DigitalOcean")
		with patch(
			"atlas.atlas.doctype.atlas_settings.atlas_settings.frappe.get_all",
			return_value=[],
		):
			settings._validate_provider_switch()  # must not raise


class TestAtlasSettingsAuthenticate(IntegrationTestCase):
	def setUp(self) -> None:
		make_provider(name="settings-auth-prov")
		self.settings = frappe.get_single("Atlas Settings")

	def test_authenticate_returns_dict(self) -> None:
		fake_impl = MagicMock()
		fake_impl.authenticate.return_value = AuthResult(
			ok=True, account_label="x@y.com", rate_limit=5000, rate_remaining=4998
		)
		with patch(
			"atlas.atlas.atlas_settings.providers.for_provider_type",
			return_value=fake_impl,
		):
			result = self.settings.authenticate()
		self.assertTrue(result["ok"])
		self.assertEqual(result["account_label"], "x@y.com")
		self.assertEqual(result["rate_limit"], 5000)

	def test_authenticate_bad_returns_error(self) -> None:
		fake_impl = MagicMock()
		fake_impl.authenticate.return_value = AuthResult(ok=False, error="401")
		with patch(
			"atlas.atlas.atlas_settings.providers.for_provider_type",
			return_value=fake_impl,
		):
			result = self.settings.authenticate()
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"], "401")


class TestAtlasSettingsRefreshCatalog(IntegrationTestCase):
	def setUp(self) -> None:
		make_provider(name="settings-refresh-prov")
		self.settings = frappe.get_single("Atlas Settings")
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

	def test_refresh_catalog_counts_inserts_updates_disables(self) -> None:
		fake_impl = MagicMock()
		fake_impl.discover.return_value = Capabilities(
			sizes=(
				SizeInfo(slug="s-2vcpu-4gb-intel", monthly_cost_usd=24),
				SizeInfo(slug="brand-new-slug", monthly_cost_usd=99),
			),
			images=(ImageInfo(slug="ubuntu-24-04-x64"),),
		)
		with patch(
			"atlas.atlas.atlas_settings.providers.for_provider_type",
			return_value=fake_impl,
		):
			result = self.settings.refresh_catalog()
		self.assertGreaterEqual(result["inserted"], 1)
		self.assertGreaterEqual(result["updated"], 2)
		self.assertGreaterEqual(result["disabled"], 1)
		self.assertEqual(
			frappe.db.get_value("Provider Size", "DigitalOcean/legacy-slug", "enabled"),
			0,
		)


class TestAtlasSettingsProvisionServer(IntegrationTestCase):
	def setUp(self) -> None:
		make_provider(name="settings-provision-prov")
		self.settings = frappe.get_single("Atlas Settings")

	def test_provision_server_inserts_and_enqueues(self) -> None:
		title = "settings-srv-1"
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
				"atlas.atlas.provisioning.providers.for_provider_type",
				return_value=fake_impl,
			),
			patch("atlas.atlas.provisioning.frappe.enqueue") as enqueue,
		):
			returned = self.settings.provision_server(title)

		server = frappe.get_doc("Server", returned)
		self.assertEqual(server.title, title)
		self.assertEqual(server.status, "Pending")
		self.assertEqual(server.provider_type, "DigitalOcean")
		self.assertEqual(server.provider_resource_id, "999")
		self.assertEqual(server.size, "DigitalOcean/s-2vcpu-4gb-intel")
		enqueue.assert_called_once()
		args, kwargs = enqueue.call_args
		self.assertEqual(args[0], "atlas.atlas.providers.worker.finish_provisioning")
		self.assertEqual(kwargs["server_name"], returned)
		frappe.db.delete("Server", {"title": title})

	def test_provision_server_rejects_duplicate(self) -> None:
		title = "settings-dup-server"
		frappe.db.delete("Server", {"title": title})
		frappe.get_doc(
			{
				"doctype": "Server",
				"title": title,
				"provider_type": "DigitalOcean",
				"provider_resource_id": "1",
				"status": "Pending",
			}
		).insert(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError) as raised:
			self.settings.provision_server(title)
		self.assertIn("already exists", str(raised.exception))
		frappe.db.delete("Server", {"title": title})


class TestAtlasSettingsProvisionServerSelfManaged(IntegrationTestCase):
	def setUp(self) -> None:
		set_atlas_settings("Self-Managed", ssh_private_key_path=_ensure_fake_ssh_key_path())
		self.settings = frappe.get_single("Atlas Settings")

	def test_provision_server_self_managed_inserts(self) -> None:
		title = "settings-self-managed-1"
		frappe.db.delete("Server", {"title": title})

		with patch("atlas.atlas.provisioning.frappe.enqueue") as enqueue:
			returned = self.settings.provision_server(
				title,
				ipv4_address="203.0.113.10",
				ipv6_address="2001:db8::1",
				ipv6_prefix="2001:db8::/64",
				ipv6_virtual_machine_range="2001:db8:dead::/64",
			)

		server = frappe.get_doc("Server", returned)
		self.assertEqual(server.title, title)
		self.assertEqual(server.status, "Pending")
		self.assertEqual(server.provider_type, "Self-Managed")
		self.assertEqual(server.ipv4_address, "203.0.113.10")
		self.assertEqual(server.ipv6_address, "2001:db8::1")
		self.assertFalse(server.provider_resource_id)
		enqueue.assert_called_once()
		frappe.db.delete("Server", {"title": title})

	def test_provision_server_self_managed_requires_addresses(self) -> None:
		title = "settings-self-managed-missing"
		frappe.db.delete("Server", {"title": title})
		with self.assertRaises(frappe.ValidationError) as raised:
			self.settings.provision_server(title)
		self.assertIn("ipv4_address", str(raised.exception))
		self.assertFalse(frappe.db.exists("Server", {"title": title}))


class TestAtlasSettingsDiscoverServers(IntegrationTestCase):
	def setUp(self) -> None:
		make_provider(name="settings-discover-prov")
		self.settings = frappe.get_single("Atlas Settings")
		frappe.db.delete("Server", {"provider_type": "DigitalOcean"})

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
		frappe.get_doc(
			{
				"doctype": "Server",
				"title": "discover-modeled",
				"provider_type": "DigitalOcean",
				"provider_resource_id": "srv-modeled",
				"status": "Pending",
			}
		).insert(ignore_permissions=True)

		fake_impl = MagicMock()
		fake_impl.list_servers.return_value = self._list_servers()
		with patch(
			"atlas.atlas.provisioning.providers.for_provider_type",
			return_value=fake_impl,
		):
			out = self.settings.discover_servers()

		by_id = {row["provider_resource_id"]: row for row in out}
		self.assertTrue(by_id["srv-modeled"]["imported"])
		self.assertFalse(by_id["srv-new"]["imported"])
		frappe.db.delete("Server", {"title": "discover-modeled"})

	def test_import_servers_inserts_pending_skips_modeled(self) -> None:
		frappe.get_doc(
			{
				"doctype": "Server",
				"title": "import-modeled",
				"provider_type": "DigitalOcean",
				"provider_resource_id": "srv-modeled",
				"status": "Pending",
			}
		).insert(ignore_permissions=True)

		fake_impl = MagicMock()
		fake_impl.list_servers.return_value = self._list_servers()
		fake_impl.describe.return_value = ProvisionResult(
			provider_resource_id="srv-new",
			size="DigitalOcean/s-4vcpu-8gb",
			image="DigitalOcean/ubuntu-24-04-x64",
			ready=True,
			networking=None,
		)
		with patch(
			"atlas.atlas.provisioning.providers.for_provider_type",
			return_value=fake_impl,
		):
			result = self.settings.import_servers(json.dumps(["srv-modeled", "srv-new"]))

		self.assertEqual(result["skipped"], ["srv-modeled"])
		self.assertEqual(len(result["imported"]), 1)
		imported = frappe.get_doc("Server", result["imported"][0]["name"])
		self.assertEqual(imported.provider_type, "DigitalOcean")
		self.assertEqual(imported.provider_resource_id, "srv-new")
		self.assertEqual(imported.status, "Pending")
		frappe.db.delete("Server", {"title": "import-modeled"})
		frappe.db.delete("Server", {"provider_resource_id": "srv-new"})
