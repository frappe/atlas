from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.base import UnsupportedProviderOperation
from atlas.atlas.core.server_providers.redfish.client import RedfishError, RedfishSystem
from atlas.atlas.core.server_providers.redfish.provider import RedfishProvider
from atlas.metal_server.core.provider_registration import register_server
from atlas.metal_server.doctype.metal_server.metal_server import MetalServer

MODULE = "atlas.metal_server.core.provider_registration"
SYSTEM = RedfishSystem("http://bmc.example/redfish/v1/Systems/machine-1", "machine-1", "Rack server", None)


class TestProviderRegistration(UnitTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.settings = SimpleNamespace(auto_spawn_metal_server=0)
		self.settings.server_provider_controller = RedfishProvider(self.settings)

	def test_reuses_an_active_record_without_changing_or_provisioning_it(self) -> None:
		server = SimpleNamespace(insert=Mock(), db_set=Mock(), _enqueue_setup_server=Mock())
		with (
			patch("atlas.atlas.core.server_providers.redfish.provider.RedfishClient") as client,
			patch(f"{MODULE}.frappe.get_doc", return_value=server),
			patch(f"{MODULE}.frappe.db.get_value", return_value="registered-server"),
			patch(f"{MODULE}.frappe.db.advisory_lock", return_value=nullcontext()),
			patch(f"{MODULE}.frappe.db.rollback"),
			patch(f"{MODULE}.frappe.db.commit"),
		):
			client.return_value.discover_system.return_value = SYSTEM
			draft = SimpleNamespace(
				settings=self.settings,
				redfish_url=SYSTEM.url,
				redfish_username="",
				get_password=Mock(return_value=""),
				provider_metadata=None,
			)
			self.assertIs(register_server(draft), server)

		server.insert.assert_not_called()
		server.db_set.assert_not_called()
		server._enqueue_setup_server.assert_not_called()

	def test_rejects_wrong_provider_and_auto_spawn_before_discovery(self) -> None:
		for provider, enabled in (
			(SimpleNamespace(is_registration_only=False), 0),
			(self.settings.server_provider_controller, 1),
		):
			settings = SimpleNamespace(server_provider_controller=provider)
			self.settings.auto_spawn_metal_server = enabled
			with (
				patch("atlas.atlas.core.server_providers.redfish.provider.RedfishClient") as client,
			):
				with self.assertRaises(RedfishError if enabled else UnsupportedProviderOperation):
					register_server(SimpleNamespace(settings=settings))
				client.assert_not_called()

	def test_redfish_insert_requires_registration_and_skips_catalogs(self) -> None:
		server = SimpleNamespace(settings=self.settings, is_new=lambda: True)
		with patch("frappe.get_doc") as catalog:
			with self.assertRaisesRegex(frappe.ValidationError, "Use Register"):
				MetalServer.before_validate(server)
			server._redfish_registration = SYSTEM
			MetalServer.before_validate(server)
			catalog.assert_not_called()

	def test_redfish_after_insert_does_not_enqueue_provisioning(self) -> None:
		server = SimpleNamespace(settings=self.settings, _enqueue_setup_server=Mock())
		MetalServer.after_insert(server)
		server._enqueue_setup_server.assert_not_called()

	def test_browser_values_cannot_mark_a_system_as_discovered(self) -> None:
		for identity in (True, {"url": SYSTEM.url, "id": SYSTEM.id, "name": SYSTEM.name, "uuid": None}):
			server = SimpleNamespace(
				settings=self.settings, is_new=lambda: True, _redfish_registration=identity
			)
			with (
				self.subTest(identity=identity),
				self.assertRaisesRegex(frappe.ValidationError, "Use Register"),
			):
				MetalServer.before_validate(server)

	def test_other_providers_require_catalogs_and_enqueue_provisioning(self) -> None:
		provider = Mock(is_registration_only=False)
		server = SimpleNamespace(
			settings=SimpleNamespace(server_provider_controller=provider),
			server_size=None,
			server_image=None,
			_enqueue_setup_server=Mock(),
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Size and Server Image"):
			MetalServer.before_validate(server)
		MetalServer.after_insert(server)
		server._enqueue_setup_server.assert_called_once_with()
