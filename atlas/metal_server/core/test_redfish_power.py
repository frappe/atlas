from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.redfish.client import RedfishError, RedfishPowerStatus
from atlas.atlas.core.server_providers.redfish.provider import RedfishProvider
from atlas.metal_server.core.redfish_power import RedfishPower
from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


class TestRedfishPower(UnitTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.provider = RedfishProvider(SimpleNamespace())
		self.server = SimpleNamespace(
			name="record",
			settings=SimpleNamespace(server_provider_controller=self.provider),
			status="Pending",
			reload=Mock(),
			_validate_power_action=Mock(),
			_provider_server_id=Mock(return_value="provider-id"),
			db_set=Mock(),
		)

	def test_refresh_saves_observed_fields_without_promoting_readiness(self) -> None:
		with (
			patch.object(self.provider, "read_power_status", return_value=RedfishPowerStatus("On", "OK")),
			patch("frappe.db.advisory_lock", return_value=nullcontext()),
			patch("frappe.db.rollback"),
			patch("frappe.db.commit") as commit,
		):
			RedfishPower(self.server).refresh()
		fields = self.server.db_set.call_args.args[0]
		self.assertEqual(fields["redfish_power_state"], "On")
		self.assertEqual(fields["redfish_health"], "OK")
		self.assertIsNotNone(fields["redfish_power_updated_on"])
		self.assertEqual(fields["status"], "Pending")
		self.server.reload.assert_called_once_with()
		self.server._validate_power_action.assert_called_once_with()
		commit.assert_called_once_with()

	def test_refresh_reconciles_off_and_transitioning_servers(self) -> None:
		for power, current, expected in (
			("Off", "Running", "Stopped"),
			("On", "Stopped", "Pending"),
			("PoweringOff", "Running", "Pending"),
			("On", "Failed", "Failed"),
		):
			self.server.status = current
			with (
				self.subTest(power=power, current=current),
				patch.object(
					self.provider, "read_power_status", return_value=RedfishPowerStatus(power, None)
				),
				patch("frappe.db.advisory_lock", return_value=nullcontext()),
				patch("frappe.db.rollback"),
				patch("frappe.db.commit"),
			):
				RedfishPower(self.server).refresh()
			self.assertEqual(self.server.db_set.call_args.args[0]["status"], expected)

	def test_failed_reads_preserve_the_previous_observation(self) -> None:
		with (
			patch.object(self.provider, "read_power_status", side_effect=RedfishError("unreachable")),
			patch("frappe.db.advisory_lock", return_value=nullcontext()),
			patch("frappe.db.rollback"),
			patch("frappe.db.commit") as commit,
		):
			with self.assertRaises(RedfishError):
				RedfishPower(self.server).refresh()
		self.server.db_set.assert_not_called()
		commit.assert_not_called()

	def test_guard_is_rechecked_after_loading_the_saved_record(self) -> None:
		self.server._validate_power_action.side_effect = frappe.PermissionError
		with (
			patch.object(self.provider, "read_power_status") as read,
			patch("frappe.db.advisory_lock", return_value=nullcontext()),
			patch("frappe.db.rollback"),
		):
			with self.assertRaises(frappe.PermissionError):
				RedfishPower(self.server).refresh()
		read.assert_not_called()

	def test_refresh_route_checks_permission_before_power_reads(self) -> None:
		self.server._validate_power_action.side_effect = frappe.PermissionError
		with patch("atlas.metal_server.doctype.metal_server.metal_server.RedfishPower") as power:
			with self.assertRaises(frappe.PermissionError):
				MetalServer.refresh_redfish_power_state(self.server)
			power.assert_not_called()
