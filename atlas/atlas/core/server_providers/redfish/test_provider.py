from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

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
