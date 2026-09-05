from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.base import ServerImageData, ServerSizeData
from atlas.server.core.catalog_sync import CatalogSynchronizer


class TestCatalogSynchronizer(UnitTestCase):
	def test_sync_server_sizes_creates_a_provider_catalog_record(self) -> None:
		provider = SimpleNamespace(
			provider_type="Test",
			fetch_server_sizes=Mock(
				return_value=(
					ServerSizeData(
						size="large",
						cpu_count=4,
						memory_mib=8192,
						disk_gib=100,
						hourly_pricing_usd_cents=10,
						monthly_pricing_usd_cents=7200,
						provider_metadata={"id": "offer-id"},
					),
				)
			),
		)
		document = Mock()

		with (
			patch(
				"atlas.server.core.catalog_sync.frappe.db",
				SimpleNamespace(exists=Mock(return_value=False)),
			),
			patch("atlas.server.core.catalog_sync.frappe.get_doc", return_value=document) as get_doc,
		):
			CatalogSynchronizer(provider).sync_server_sizes()

		values = get_doc.call_args.args[0]
		self.assertEqual(values["provider_type"], "Test")
		self.assertEqual(values["memory_mib"], 8192)
		document.insert.assert_called_once_with(ignore_permissions=True)

	def test_sync_server_images_skips_unchanged_metadata(self) -> None:
		provider = SimpleNamespace(
			provider_type="Test",
			fetch_server_images=Mock(
				return_value=(
					ServerImageData(
						image="Ubuntu_26.04",
						os="Ubuntu",
						version="26.04",
						provider_metadata={"id": "image-id"},
					),
				)
			),
		)
		document = SimpleNamespace(provider_metadata=frappe.as_json({"id": "image-id"}), save=Mock())

		with (
			patch(
				"atlas.server.core.catalog_sync.frappe.db",
				SimpleNamespace(exists=Mock(return_value=True)),
			),
			patch("atlas.server.core.catalog_sync.frappe.get_doc", return_value=document),
		):
			CatalogSynchronizer(provider).sync_server_images()

		document.save.assert_not_called()
