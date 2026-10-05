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

	def test_settings_validation_does_not_contact_the_bmc(self) -> None:
		with patch("requests.request") as request:
			for enabled in (0, 1):
				provider = RedfishProvider(SimpleNamespace(auto_spawn_metal_server=enabled))
				if enabled:
					with self.assertRaises(RedfishError):
						provider.validate_settings()
				else:
					provider.validate_settings()

		request.assert_not_called()

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
