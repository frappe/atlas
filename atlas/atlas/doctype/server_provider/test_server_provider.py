from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.tests.fixtures import make_provider


class TestServerProvider(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider()

	def test_test_connection_ok(self) -> None:
		fake_client = MagicMock()
		fake_client.account.return_value = {"email": "ok@example.com"}
		with patch(
			"atlas.atlas.doctype.server_provider.server_provider.DigitalOceanClient",
			return_value=fake_client,
		):
			result = self.provider.test_connection()
		self.assertTrue(result["ok"])
		self.assertEqual(result["email"], "ok@example.com")

	def test_preview_cost_returns_static_dict_value(self) -> None:
		preview = self.provider.preview_cost()
		self.assertEqual(preview["provider_type"], "DigitalOcean")
		self.assertEqual(preview["region"], "blr1")
		self.assertEqual(preview["size"], "s-2vcpu-4gb-intel")
		self.assertEqual(preview["monthly_cost_usd"], 24)
		self.assertEqual(preview["currency"], "USD")

	def test_preview_cost_returns_none_for_unknown_size(self) -> None:
		self.provider.default_size = "no-such-size"
		preview = self.provider.preview_cost()
		self.assertIsNone(preview["monthly_cost_usd"])

	def test_test_connection_bad(self) -> None:
		from atlas.atlas.digitalocean import DigitalOceanError
		fake_client = MagicMock()
		fake_client.account.side_effect = DigitalOceanError("401")
		with patch(
			"atlas.atlas.doctype.server_provider.server_provider.DigitalOceanClient",
			return_value=fake_client,
		):
			with self.assertRaises(DigitalOceanError):
				self.provider.test_connection()

	def test_credential_check_ok_returns_rate_limit(self) -> None:
		fake_client = MagicMock()
		fake_client.verify_credentials.return_value = {
			"email": "ok@example.com",
			"rate_limit": 5000,
			"rate_remaining": 4998,
		}
		with patch(
			"atlas.atlas.doctype.server_provider.server_provider.DigitalOceanClient",
			return_value=fake_client,
		):
			result = self.provider.credential_check()
		self.assertTrue(result["ok"])
		self.assertEqual(result["email"], "ok@example.com")
		self.assertEqual(result["rate_limit"], 5000)
		self.assertEqual(result["rate_remaining"], 4998)

	def test_credential_check_bad_returns_error_without_raising(self) -> None:
		from atlas.atlas.digitalocean import DigitalOceanError
		fake_client = MagicMock()
		fake_client.verify_credentials.side_effect = DigitalOceanError(
			"GET /account -> 401: Unauthorized"
		)
		with patch(
			"atlas.atlas.doctype.server_provider.server_provider.DigitalOceanClient",
			return_value=fake_client,
		):
			result = self.provider.credential_check()
		self.assertFalse(result["ok"])
		self.assertIn("401", result["error"])

	def test_provision_server_inserts_and_enqueues(self) -> None:
		from atlas.atlas.doctype.server_provider import server_provider as module

		title = "test-srv-1"
		frappe.db.delete("Server", {"title": title})

		fake_client = MagicMock()
		fake_client.create_droplet.return_value = {"id": 999}
		with patch.object(module, "DigitalOceanClient", return_value=fake_client):
			with patch.object(module.frappe, "enqueue") as enqueue:
				returned = self.provider.provision_server(title)

		# `provision_server` returns the new row's UUID `name`, not the title.
		server = frappe.get_doc("Server", returned)
		self.assertEqual(server.title, title)
		self.assertEqual(server.status, "Pending")
		self.assertEqual(server.provider_resource_id, "999")
		enqueue.assert_called_once()
		_, kwargs = enqueue.call_args
		self.assertEqual(kwargs["server_name"], returned)
		frappe.db.delete("Server", {"title": title})

	def test_finish_provisioning_marks_broken_on_bootstrap_failure(self) -> None:
		from atlas.atlas.doctype.server_provider import server_provider as module

		title = "test-srv-broken"
		frappe.db.delete("Server", {"title": title})
		server = frappe.get_doc({
			"doctype": "Server",
			"title": title,
			"provider": self.provider.name,
			"provider_resource_id": "1234",
			"status": "Pending",
		}).insert(ignore_permissions=True)

		fake_droplet = {
			"id": 1234,
			"status": "active",
			"networks": {
				"v4": [{"type": "public", "ip_address": "1.2.3.4"}],
				"v6": [{"type": "public", "ip_address": "2a03:b0c0:abcd:1234::1", "netmask": 64}],
			},
		}
		fake_client = MagicMock()
		fake_client.wait_for_active.return_value = fake_droplet

		with patch.object(module, "DigitalOceanClient", return_value=fake_client):
			with patch.object(module, "wait_for_ssh"):
				with patch(
					"atlas.atlas.doctype.server.server.Server.bootstrap",
					side_effect=frappe.ValidationError("bootstrap broke"),
				):
					with self.assertRaises(frappe.ValidationError):
						module.finish_provisioning(server.name)
		server.reload()
		self.assertEqual(server.status, "Broken")
		frappe.db.delete("Server", {"title": title})

	def test_provision_server_rejects_duplicate(self) -> None:
		from atlas.atlas.doctype.server_provider import server_provider as module

		title = "dup-server"
		frappe.db.delete("Server", {"title": title})
		frappe.get_doc({
			"doctype": "Server",
			"title": title,
			"provider": self.provider.name,
			"provider_resource_id": "1",
			"status": "Pending",
		}).insert(ignore_permissions=True)

		fake_client = MagicMock()
		with patch.object(module, "DigitalOceanClient", return_value=fake_client):
			with self.assertRaises(frappe.ValidationError) as raised:
				self.provider.provision_server(title)
		self.assertIn("already exists", str(raised.exception))
		fake_client.create_droplet.assert_not_called()
		frappe.db.delete("Server", {"title": title})

	def test_finish_provisioning_marks_active_on_success(self) -> None:
		from atlas.atlas.doctype.server_provider import server_provider as module

		title = "test-srv-ok"
		frappe.db.delete("Server", {"title": title})
		server = frappe.get_doc({
			"doctype": "Server",
			"title": title,
			"provider": self.provider.name,
			"provider_resource_id": "4242",
			"status": "Pending",
		}).insert(ignore_permissions=True)

		fake_droplet = {
			"id": 4242,
			"status": "active",
			"networks": {
				"v4": [{"type": "public", "ip_address": "5.6.7.8"}],
				"v6": [{"type": "public", "ip_address": "2a03:b0c0:abcd:5678::1", "netmask": 64}],
			},
		}
		fake_client = MagicMock()
		fake_client.wait_for_active.return_value = fake_droplet

		with patch.object(module, "DigitalOceanClient", return_value=fake_client):
			with patch.object(module, "wait_for_ssh"):
				with patch(
					"atlas.atlas.doctype.server.server.Server.bootstrap",
					return_value="task-name",
				):
					module.finish_provisioning(server.name)

		server.reload()
		self.assertEqual(server.status, "Active")
		self.assertEqual(server.ipv4_address, "5.6.7.8")
		self.assertEqual(server.ipv6_address, "2a03:b0c0:abcd:5678::1")
		self.assertEqual(server.ipv6_prefix, "2a03:b0c0:abcd:5678::/64")
		frappe.db.delete("Server", {"title": title})


class TestSelfManagedProvider(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider(
			name="test-self-managed",
			provider_type="Self-Managed",
			api_token=None,
			ssh_key_id=None,
			default_region=None,
			default_size=None,
			default_image=None,
		)

	def test_validate_requires_do_fields_only_for_digitalocean(self) -> None:
		self.assertEqual(self.provider.provider_type, "Self-Managed")
		self.assertFalse(self.provider.api_token)
		self.assertFalse(self.provider.default_region)

	def test_validate_blocks_digitalocean_missing_fields(self) -> None:
		from atlas.tests.fixtures import _ensure_fake_ssh_key_path

		name = "incomplete-do"
		frappe.db.delete("Server Provider", {"provider_name": name})
		with self.assertRaises(frappe.ValidationError) as raised:
			frappe.get_doc({
				"doctype": "Server Provider",
				"provider_name": name,
				"provider_type": "DigitalOcean",
				"ssh_private_key_path": _ensure_fake_ssh_key_path(),
			}).insert(ignore_permissions=True)
		self.assertIn("DigitalOcean providers require", str(raised.exception))

	def test_provision_server_self_managed_inserts_and_enqueues(self) -> None:
		from atlas.atlas.doctype.server_provider import server_provider as module

		title = "self-managed-srv-1"
		frappe.db.delete("Server", {"title": title})

		with patch.object(module.frappe, "enqueue") as enqueue:
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
		self.assertEqual(server.ipv6_prefix, "2001:db8::/64")
		self.assertEqual(server.ipv6_virtual_machine_range, "2001:db8:dead::/64")
		self.assertFalse(server.provider_resource_id)
		enqueue.assert_called_once()
		_, kwargs = enqueue.call_args
		self.assertEqual(kwargs["server_name"], returned)
		frappe.db.delete("Server", {"title": title})

	def test_provision_server_self_managed_requires_addresses(self) -> None:
		title = "self-managed-missing"
		frappe.db.delete("Server", {"title": title})
		with self.assertRaises(frappe.ValidationError) as raised:
			self.provider.provision_server(title)
		self.assertIn("ipv4_address", str(raised.exception))
		self.assertFalse(frappe.db.exists("Server", {"title": title}))

	def test_finish_provisioning_self_managed_skips_droplet_wait(self) -> None:
		from atlas.atlas.doctype.server_provider import server_provider as module

		title = "self-managed-finish"
		frappe.db.delete("Server", {"title": title})
		server = frappe.get_doc({
			"doctype": "Server",
			"title": title,
			"provider": self.provider.name,
			"status": "Pending",
			"ipv4_address": "203.0.113.20",
			"ipv6_address": "2001:db8::2",
			"ipv6_prefix": "2001:db8::/64",
			"ipv6_virtual_machine_range": "2001:db8:beef::/64",
		}).insert(ignore_permissions=True)

		with patch.object(module, "DigitalOceanClient") as do_client:
			with patch.object(module, "wait_for_ssh") as wait_ssh:
				with patch(
					"atlas.atlas.doctype.server.server.Server.bootstrap",
					return_value="task-name",
				):
					module.finish_provisioning(server.name)

		do_client.assert_not_called()
		wait_ssh.assert_called_once()
		server.reload()
		self.assertEqual(server.status, "Active")
		self.assertEqual(server.ipv4_address, "203.0.113.20")
		self.assertEqual(server.ipv6_virtual_machine_range, "2001:db8:beef::/64")
		frappe.db.delete("Server", {"title": title})

	def test_test_connection_rejected_for_self_managed(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			self.provider.test_connection()

	def test_credential_check_skipped_for_self_managed(self) -> None:
		result = self.provider.credential_check()
		self.assertTrue(result["ok"])
		self.assertTrue(result["skipped"])


class TestServerProviderImmutability(IntegrationTestCase):
	def setUp(self) -> None:
		# Reset the provider so each test starts from `is_active=1`. The
		# fixture's "create if not exists" path otherwise leaves a row
		# archived by an earlier test, which breaks the archive-already
		# branch below.
		frappe.db.delete("Server Provider", {"provider_name": "test-immutable-provider"})
		self.provider = make_provider(name="test-immutable-provider")

	def test_immutability_blocks_validate_for_credential_fields(self) -> None:
		self.provider.api_token = "dop_v1_new"
		with self.assertRaises(frappe.ValidationError) as raised:
			self.provider.save(ignore_permissions=True)
		self.assertIn("api_token is immutable", str(raised.exception))

	def test_immutability_blocks_validate_for_defaults(self) -> None:
		self.provider.reload()
		self.provider.default_region = "nyc3"
		with self.assertRaises(frappe.ValidationError) as raised:
			self.provider.save(ignore_permissions=True)
		self.assertIn("default_region is immutable", str(raised.exception))

	def test_archive_sets_is_active_zero(self) -> None:
		self.provider.reload()
		self.assertEqual(self.provider.is_active, 1)
		self.provider.archive()
		self.assertEqual(
			frappe.db.get_value("Server Provider", self.provider.name, "is_active"),
			0,
		)

	def test_archive_throws_when_already_archived(self) -> None:
		self.provider.reload()
		self.provider.archive()
		# Reload so the in-memory doc reflects is_active=0
		self.provider.reload()
		with self.assertRaises(frappe.ValidationError) as raised:
			self.provider.archive()
		self.assertIn("already archived", str(raised.exception))


class TestGetSshKeyFromDisk(IntegrationTestCase):
	def test_returns_file_contents(self) -> None:
		import pathlib
		import tempfile

		from atlas.atlas.secrets import get_ssh_key_from_disk

		with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as handle:
			handle.write("-----BEGIN OPENSSH PRIVATE KEY-----\ncontents\n")
			path = handle.name
		try:
			self.assertEqual(
				get_ssh_key_from_disk(path),
				"-----BEGIN OPENSSH PRIVATE KEY-----\ncontents\n",
			)
		finally:
			pathlib.Path(path).unlink(missing_ok=True)

	def test_throws_when_missing(self) -> None:
		from atlas.atlas.secrets import get_ssh_key_from_disk

		with self.assertRaises(frappe.ValidationError):
			get_ssh_key_from_disk("/nonexistent/key.pem")
