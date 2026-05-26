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

	def test_provision_server_inserts_and_enqueues(self) -> None:
		from atlas.atlas.doctype.server_provider import server_provider as module

		server_name = "test-srv-1"
		frappe.db.delete("Server", {"server_name": server_name})

		fake_client = MagicMock()
		fake_client.create_droplet.return_value = {"id": 999}
		with patch.object(module, "DigitalOceanClient", return_value=fake_client):
			with patch.object(module.frappe, "enqueue") as enqueue:
				returned = self.provider.provision_server(server_name)

		self.assertEqual(returned, server_name)
		server = frappe.get_doc("Server", server_name)
		self.assertEqual(server.status, "Pending")
		self.assertEqual(server.provider_resource_id, "999")
		enqueue.assert_called_once()
		_, kwargs = enqueue.call_args
		self.assertEqual(kwargs["server_name"], server_name)
		self.assertEqual(kwargs["droplet_id"], 999)
		frappe.db.delete("Server", {"server_name": server_name})

	def test_finish_provisioning_marks_broken_on_bootstrap_failure(self) -> None:
		from atlas.atlas.doctype.server_provider import server_provider as module

		server_name = "test-srv-broken"
		frappe.db.delete("Server", {"server_name": server_name})
		server = frappe.get_doc({
			"doctype": "Server",
			"server_name": server_name,
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
						module.finish_provisioning(server_name, 1234)
		server.reload()
		self.assertEqual(server.status, "Broken")
		frappe.db.delete("Server", {"server_name": server_name})
