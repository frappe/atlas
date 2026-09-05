from __future__ import annotations

from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.base import ServerProvider, UnsupportedProviderOperation
from atlas.atlas.core.server_providers.registry import get_server_provider, register


class TestServerProviderRegistry(UnitTestCase):
	def test_register_rejects_a_duplicate_provider_type(self) -> None:
		first_provider = type("FirstProvider", (), {"provider_type": "Test"})
		second_provider = type("SecondProvider", (), {"provider_type": "Test"})

		with patch("atlas.atlas.core.server_providers.registry._REGISTRY", {}):
			register(first_provider)
			with self.assertRaises(ValueError):
				register(second_provider)

	def test_unknown_provider_raises_a_plain_domain_error(self) -> None:
		with (
			patch("atlas.atlas.core.server_providers.registry._REGISTRY", {}),
			patch("atlas.atlas.core.server_providers.registry.load_implementations"),
			self.assertRaises(ValueError),
		):
			get_server_provider("Unknown")

	def test_optional_ip_address_operation_has_a_distinct_error(self) -> None:
		with self.assertRaises(UnsupportedProviderOperation) as raised:
			ServerProvider.reserve_public_ipv4_address(Mock())

		self.assertEqual(raised.exception.code, "unsupported_provider_operation")
