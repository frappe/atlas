from __future__ import annotations

import json
from types import SimpleNamespace

from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.scaleway.catalog import ScalewayCatalog
from atlas.atlas.core.server_providers.scaleway.client import ScalewayError


class TestScalewayCatalog(UnitTestCase):
	def test_get_server_images_keeps_supported_os_versions(self) -> None:
		images = ScalewayCatalog().get_server_images([{"name": "Ubuntu", "version": "24.04 LTS"}])

		self.assertEqual(images[0].os, "Ubuntu")
		self.assertEqual(images[0].version, "24.04")

	def test_get_server_images_skips_unsupported_os_versions(self) -> None:
		images = ScalewayCatalog().get_server_images([{"name": "Ubuntu", "version": "13"}])

		self.assertEqual(images, ())

	def test_get_offer_id_uses_the_subscription_period(self) -> None:
		size = SimpleNamespace(
			name="Scaleway/EM-A410X",
			provider_metadata=json.dumps({"hourly": {"id": "hourly-id", "monthly_offer_id": "monthly-id"}}),
		)

		offer_id = ScalewayCatalog().get_offer_id(size, "monthly")

		self.assertEqual(offer_id, "monthly-id")

	def test_get_private_network_option_id_uses_offer_metadata(self) -> None:
		size = SimpleNamespace(
			name="Scaleway/EM-A410X",
			provider_metadata=json.dumps(
				{"hourly": {"options": [{"id": "private-network-id", "private_network": {}}]}}
			),
		)

		option_id = ScalewayCatalog().get_private_network_option_id(size, "hourly")

		self.assertEqual(option_id, "private-network-id")

	def test_get_private_network_option_id_matches_the_subscription_period(self) -> None:
		size = SimpleNamespace(
			name="Scaleway/EM-A410X",
			provider_metadata=json.dumps(
				{
					"hourly": {"options": [{"id": "hourly-option", "private_network": {}}]},
					"monthly": {"options": [{"id": "monthly-option", "private_network": {}}]},
				}
			),
		)

		option_id = ScalewayCatalog().get_private_network_option_id(size, "monthly")

		self.assertEqual(option_id, "monthly-option")

	def test_get_private_network_option_id_rejects_a_missing_period(self) -> None:
		"""Sending an hourly option with a monthly offer makes Scaleway fail."""
		size = SimpleNamespace(
			name="Scaleway/EM-A410X",
			provider_metadata=json.dumps(
				{"hourly": {"options": [{"id": "hourly-option", "private_network": {}}]}}
			),
		)

		with self.assertRaises(ScalewayError):
			ScalewayCatalog().get_private_network_option_id(size, "monthly")
