from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.base import ServerPowerStatus, UnsupportedProviderOperation
from atlas.atlas.core.server_providers.generic.bmc.client import RedfishError
from atlas.atlas.core.server_providers.generic.bmc.driver import BMCDriver
from atlas.atlas.core.server_providers.generic.provider import GenericProvider
from atlas.metal_server.doctype.metal_server.metal_server import MetalServer, poweroff_redfish_server

FIELDS = ("redfish_power_state", "redfish_health")


class TestBMCVirtualFields(UnitTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.previous_cache = frappe.local.request_cache
		frappe.local.request_cache = defaultdict(dict)
		self.provider = BMCDriver(SimpleNamespace())
		url = "http://bmc.example/redfish/v1/Systems/host-1"
		self.server = MetalServer(
			{
				"doctype": "Metal Server",
				"name": "record",
				"status": "Running",
				"redfish_url": url,
				"provider_server_id": "redfish-" + sha256(url.encode()).hexdigest()[:32],
				"redfish_power_state": "Off",
				"redfish_health": "Critical",
			}
		)
		self.server._settings = SimpleNamespace(server_provider_controller=self.provider)

	def tearDown(self) -> None:
		frappe.local.request_cache = self.previous_cache
		super().tearDown()

	def test_serialization_shares_one_live_observation_and_does_not_write(self) -> None:
		class RegisteredProvider(GenericProvider):
			is_registration_only = True

		for provider in (self.provider, RegisteredProvider(SimpleNamespace())):
			self.server._settings.server_provider_controller = provider
			frappe.local.request_cache = defaultdict(dict)

			with (
				patch.object(
					provider, "read_power_status", return_value=ServerPowerStatus("On", "OK")
				) as read,
				patch.object(self.server, "db_set") as save,
				patch("frappe.db.commit") as commit,
				patch("requests.post") as write,
			):
				values = self.server.as_dict()
				self.assertEqual(values["redfish_power_state"], "On")
				self.assertEqual(values["redfish_health"], "OK")
				self.assertEqual(self.server.redfish_power_state, "On")
				self.assertEqual(self.server.redfish_health, "OK")
			read.assert_called_once_with(self.server.provider_server_id)
			self.assertEqual(self.server.status, "Running")
			save.assert_not_called()
			commit.assert_not_called()
			write.assert_not_called()

	def test_a_new_request_reads_fresh_values_without_reconciling_lifecycle(self) -> None:
		with patch.object(
			self.provider,
			"read_power_status",
			side_effect=[ServerPowerStatus("On", "OK"), ServerPowerStatus("Off", None)],
		) as read:
			first = self.server.as_dict()
			frappe.local.request_cache = defaultdict(dict)
			second = self.server.as_dict()
		self.assertEqual(read.call_count, 2)
		self.assertEqual((first["redfish_power_state"], second["redfish_power_state"]), ("On", "Off"))
		self.assertIsNone(second["redfish_health"])
		self.assertEqual(self.server.status, "Running")

	def test_failure_returns_blank_fields_with_one_error_and_recovers_on_the_next_request(self) -> None:
		with patch.object(
			self.provider,
			"read_power_status",
			side_effect=[RedfishError("Redfish returned HTTP 401"), ServerPowerStatus("On", "OK")],
		) as read:
			failed = self.server.as_dict()
			for field in FIELDS:
				self.assertIsNone(failed[field])
			self.assertEqual(failed["__onload"]["redfish_power_error"], "Redfish returned HTTP 401")
			self.assertEqual(read.call_count, 1)
			frappe.local.request_cache = defaultdict(dict)
			recovered = self.server.as_dict()
		self.assertEqual(read.call_count, 2)
		self.assertEqual(recovered["redfish_power_state"], "On")
		self.assertIsNone(recovered["__onload"]["redfish_power_error"])

	def test_unsaved_deleted_unregistered_and_other_provider_records_do_not_contact_redfish(self) -> None:
		for values in (
			{"__islocal": 1},
			{"status": "Deleted"},
			{"redfish_url": ""},
			{"provider_server_id": ""},
			{},
		):
			with self.subTest(values=values), patch.object(self.provider, "read_power_status") as read:
				frappe.local.request_cache = defaultdict(dict)
				server = MetalServer(self.server.__dict__ | values)
				server._settings = SimpleNamespace(
					server_provider_controller=self.provider
					if values
					else SimpleNamespace(is_registration_only=False)
				)
				for field in FIELDS:
					self.assertIsNone(getattr(server, field))
				read.assert_not_called()

	def test_permissions_are_checked_before_remote_reads(self) -> None:
		for denied in ("role", "document"):
			with (
				self.subTest(denied=denied),
				patch("frappe.only_for", side_effect=frappe.PermissionError if denied == "role" else None),
				patch.object(
					self.server,
					"check_permission",
					side_effect=frappe.PermissionError if denied == "document" else None,
				),
				patch.object(self.provider, "read_power_status") as read,
			):
				with self.assertRaises(frappe.PermissionError):
					self.server.read_provider_power_status()
				read.assert_not_called()

	def test_virtual_fields_are_excluded_from_database_values(self) -> None:
		with patch.object(self.provider, "read_power_status") as read:
			persisted = self.server.get_valid_dict(ignore_virtual=True)
		for field in FIELDS:
			self.assertTrue(self.server.meta.get_field(field).is_virtual)
			self.assertNotIn(field, persisted)
		read.assert_not_called()

	def test_shutdown_endpoint_refuses_insufficient_permissions_and_other_providers(self) -> None:
		server = SimpleNamespace(check_permission=Mock(), poweroff_server=Mock())
		for denied in ("role", "document", "provider"):
			with (
				self.subTest(denied=denied),
				patch("frappe.get_doc", return_value=server),
				patch("frappe.only_for", side_effect=frappe.PermissionError if denied == "role" else None),
			):
				server.check_permission.side_effect = frappe.PermissionError if denied == "document" else None
				server.settings = SimpleNamespace(
					server_provider_controller=SimpleNamespace(is_registration_only=False)
				)
				with self.assertRaises(
					UnsupportedProviderOperation if denied == "provider" else frappe.PermissionError
				):
					poweroff_redfish_server("record")
				server.poweroff_server.assert_not_called()
