from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.redfish import RedfishError, RedfishProvider
from atlas.atlas.core.server_providers.registry import get_server_provider


class TestRedfishProvider(UnitTestCase):
	def test_registry_returns_the_redfish_provider(self) -> None:
		settings = SimpleNamespace(auto_spawn_metal_server=0)

		self.assertIsInstance(get_server_provider("Redfish", settings=settings), RedfishProvider)

	def test_settings_accept_manual_host_management(self) -> None:
		provider = RedfishProvider(SimpleNamespace(auto_spawn_metal_server=0))

		self.assertIsNone(provider.validate_settings())

	def test_settings_reject_automatic_host_creation(self) -> None:
		provider = RedfishProvider(SimpleNamespace(auto_spawn_metal_server=1))

		with self.assertRaisesRegex(RedfishError, "automatically") as raised:
			provider.validate_settings()

		self.assertEqual(raised.exception.code, "provider_error")
		self.assertFalse(raised.exception.is_retryable)

	def test_credentials_check_every_active_redfish_system_with_saved_passwords(self) -> None:
		urls = [f"http://bmc.example/redfish/v1/Systems/host-{index}" for index in (1, 2)]
		provider_ids = ["redfish-" + sha256(url.encode()).hexdigest()[:32] for url in urls]
		servers = [
			SimpleNamespace(
				redfish_url=url, redfish_username="operator", get_password=Mock(return_value="test-password")
			)
			for url in urls
		]
		responses = [
			Mock(
				status_code=200,
				json=Mock(
					return_value={
						"@odata.type": "#ComputerSystem.v1_20_0.ComputerSystem",
						"@odata.id": url,
						"Id": url.rsplit("/", 1)[-1],
						"Name": "Rack server",
					}
				),
			)
			for url in urls
		]
		with (
			patch("frappe.get_all", return_value=provider_ids) as records,
			patch("frappe.db.get_value", side_effect=["record-1", "record-2"]),
			patch("frappe.get_doc", side_effect=servers),
			patch("requests.get", side_effect=responses) as read,
			patch("requests.post") as write,
			patch("frappe.db.set_value") as save,
			patch("frappe.db.commit") as commit,
		):
			self.assertTrue(RedfishProvider(SimpleNamespace()).validate_credentials())
		records.assert_called_once_with(
			"Metal Server",
			filters={"provider_server_id": ["like", "redfish-%"], "status": ["!=", "Deleted"]},
			pluck="provider_server_id",
			order_by="name",
		)
		self.assertEqual([call.args[0] for call in read.call_args_list], urls)
		for call, server in zip(read.call_args_list, servers, strict=True):
			self.assertEqual(call.kwargs["auth"], ("operator", "test-password"))
			self.assertEqual(call.kwargs["timeout"], 10)
			self.assertFalse(call.kwargs["allow_redirects"])
			server.get_password.assert_called_once_with("redfish_password", raise_exception=False)
		write.assert_not_called()
		save.assert_not_called()
		commit.assert_not_called()

	def test_credentials_refuse_an_empty_registration_set_without_network_access(self) -> None:
		with patch("frappe.get_all", return_value=[]), patch("requests.get") as read:
			with self.assertRaisesRegex(RedfishError, "Register a Redfish Metal Server"):
				RedfishProvider(SimpleNamespace()).validate_credentials()
		read.assert_not_called()

	def test_credentials_fail_on_the_first_inaccessible_system(self) -> None:
		provider = RedfishProvider(SimpleNamespace())
		for status in (401, 403, 500):
			with (
				self.subTest(status=status),
				patch("frappe.get_all", return_value=["first", "second"]),
				patch.object(provider, "_client") as client,
			):
				client.return_value.discover_system.side_effect = RedfishError(
					f"Redfish returned HTTP {status}"
				)
				with self.assertRaisesRegex(RedfishError, f"HTTP {status}"):
					provider.validate_credentials()
			client.assert_called_once_with("first")

	def test_credentials_reject_a_different_system_in_a_successful_response(self) -> None:
		provider = RedfishProvider(SimpleNamespace())
		with (
			patch("frappe.get_all", return_value=["provider-id"]),
			patch.object(provider, "_client") as client,
		):
			client.return_value.url = "http://bmc.example/redfish/v1/Systems/host-1"
			client.return_value.discover_system.return_value.url = (
				"http://bmc.example/redfish/v1/Systems/host-2"
			)
			with self.assertRaisesRegex(RedfishError, "different system"):
				provider.validate_credentials()

	def test_power_reads_use_saved_credentials_and_registered_identity(self) -> None:
		url = "http://bmc.example/redfish/v1/Systems/host-1"
		provider_id = "redfish-" + sha256(url.encode()).hexdigest()[:32]
		server = SimpleNamespace(
			redfish_url=url, redfish_username="operator", get_password=Mock(return_value="test-password")
		)
		with (
			patch("frappe.db.get_value", return_value="record"),
			patch("frappe.get_doc", return_value=server),
			patch("requests.get") as read,
		):
			read.return_value.status_code = 200
			read.return_value.json.return_value = {
				"@odata.type": "#ComputerSystem.v1_20_0.ComputerSystem",
				"@odata.id": url,
				"PowerState": "On",
			}
			status = RedfishProvider(SimpleNamespace()).read_power_status(provider_id)
		self.assertEqual(status.power_state, "On")
		self.assertEqual(read.call_args.kwargs["auth"], ("operator", "test-password"))
		server.get_password.assert_called_once_with("redfish_password", raise_exception=False)

	def test_power_reads_refuse_unregistered_or_changed_systems_before_network_access(self) -> None:
		for name, url in (
			(None, "http://bmc.example/redfish/v1/Systems/host-1"),
			("record", None),
			("record", "http://other.example/redfish/v1/Systems/host-1"),
		):
			server = SimpleNamespace(
				redfish_url=url, redfish_username="", get_password=Mock(return_value=None)
			)
			with (
				self.subTest(name=name, url=url),
				patch("frappe.db.get_value", return_value=name),
				patch("frappe.get_doc", return_value=server),
				patch("requests.get") as read,
			):
				with self.assertRaises(RedfishError):
					RedfishProvider(SimpleNamespace()).read_power_status("unverified-id")
				read.assert_not_called()
