from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.base import ServerPowerAction
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

	def test_power_on_stores_an_observation_only_after_the_action_succeeds(self) -> None:
		self.server.status = "Stopped"
		with (
			patch.object(self.provider, "set_power_state") as change,
			patch.object(self.provider, "read_power_status", return_value=RedfishPowerStatus("On", "OK")),
			patch("frappe.db.advisory_lock", return_value=nullcontext()),
			patch("frappe.db.rollback"),
			patch("frappe.db.commit"),
		):
			RedfishPower(self.server).set_power_state(ServerPowerAction.START)
		change.assert_called_once_with("provider-id", ServerPowerAction.START)
		self.assertEqual(self.server.db_set.call_args.args[0]["status"], "Pending")

	def test_failed_power_on_does_not_save_an_optimistic_state(self) -> None:
		with (
			patch.object(self.provider, "set_power_state", side_effect=RedfishError("outcome unknown")),
			patch.object(self.provider, "read_power_status") as read,
			patch("frappe.db.advisory_lock", return_value=nullcontext()),
			patch("frappe.db.rollback"),
			patch("frappe.db.commit") as commit,
		):
			with self.assertRaises(RedfishError):
				RedfishPower(self.server).set_power_state(ServerPowerAction.START)
		read.assert_not_called()
		self.server.db_set.assert_not_called()
		commit.assert_not_called()

	def test_redfish_power_on_route_delegates_to_the_power_owner(self) -> None:
		with patch("atlas.metal_server.doctype.metal_server.metal_server.RedfishPower") as power:
			MetalServer.poweron_server(self.server)
		power.assert_called_once_with(self.server)
		power.return_value.set_power_state.assert_called_once_with(ServerPowerAction.START)

	def test_shutdown_delegates_the_saved_provider_identity(self) -> None:
		with (
			patch.object(self.provider, "set_power_state") as change,
			patch.object(self.provider, "read_power_status", return_value=RedfishPowerStatus("Off", "OK")),
			patch("frappe.db.advisory_lock", return_value=nullcontext()),
			patch("frappe.db.rollback"),
			patch("frappe.db.commit"),
		):
			RedfishPower(self.server).set_power_state(ServerPowerAction.STOP)
		change.assert_called_once_with("provider-id", ServerPowerAction.STOP)

	def test_shutdown_route_checks_permission_and_delegates_to_the_power_owner(self) -> None:
		with patch("atlas.metal_server.doctype.metal_server.metal_server.RedfishPower") as power:
			MetalServer.poweroff_server(self.server)
			power.return_value.set_power_state.assert_called_once_with(ServerPowerAction.STOP)
			power.reset_mock()
			self.server._validate_power_action.side_effect = frappe.PermissionError
			with self.assertRaises(frappe.PermissionError):
				MetalServer.poweroff_server(self.server)
			power.assert_not_called()

	def test_reboot_invalidates_running_readiness(self) -> None:
		self.server.status = "Running"
		with (
			patch.object(self.provider, "set_power_state") as change,
			patch.object(self.provider, "read_power_status", return_value=RedfishPowerStatus("On", "OK")),
			patch("frappe.db.advisory_lock", return_value=nullcontext()),
			patch("frappe.db.rollback"),
			patch("frappe.db.commit"),
		):
			RedfishPower(self.server).set_power_state(ServerPowerAction.REBOOT)
		change.assert_called_once_with("provider-id", ServerPowerAction.REBOOT)
		self.assertEqual(self.server.db_set.call_args.args[0]["status"], "Pending")

	def test_reboot_route_checks_permission_and_uses_the_power_owner(self) -> None:
		with patch("atlas.metal_server.doctype.metal_server.metal_server.RedfishPower") as power:
			MetalServer.reboot_server(self.server)
			power.return_value.set_power_state.assert_called_once_with(ServerPowerAction.REBOOT)
			power.reset_mock()
			self.server._validate_power_action.side_effect = frappe.PermissionError
			with self.assertRaises(frappe.PermissionError):
				MetalServer.reboot_server(self.server)
			power.assert_not_called()

	def test_failed_observation_after_reboot_does_not_save_a_result(self) -> None:
		with (
			patch.object(self.provider, "set_power_state") as change,
			patch.object(self.provider, "read_power_status", side_effect=RedfishError("unreachable")),
			patch("frappe.db.advisory_lock", return_value=nullcontext()),
			patch("frappe.db.rollback"),
			patch("frappe.db.commit") as commit,
		):
			with self.assertRaises(RedfishError):
				RedfishPower(self.server).set_power_state(ServerPowerAction.REBOOT)
		change.assert_called_once()
		self.server.db_set.assert_not_called()
		commit.assert_not_called()

	def test_power_result_commits_before_releasing_the_server_lock(self) -> None:
		events = []
		lock = Mock()
		lock.__enter__ = Mock(side_effect=lambda: events.append("lock"))
		lock.__exit__ = Mock(side_effect=lambda *_: events.append("unlock"))
		self.server.reload.side_effect = lambda: events.append("reload")
		with (
			patch.object(self.provider, "set_power_state", side_effect=lambda *_: events.append("reset")),
			patch.object(self.provider, "read_power_status", return_value=RedfishPowerStatus("On", "OK")),
			patch("frappe.db.advisory_lock", return_value=lock),
			patch("frappe.db.rollback", side_effect=lambda: events.append("rollback")),
			patch("frappe.db.commit", side_effect=lambda: events.append("commit")),
		):
			RedfishPower(self.server).set_power_state(ServerPowerAction.REBOOT)
		self.assertEqual(events, ["lock", "rollback", "reload", "reset", "commit", "unlock"])
