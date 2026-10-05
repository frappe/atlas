from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.base import ServerPowerAction, ServerPowerStatus
from atlas.atlas.core.server_providers.generic.bmc.client import RedfishError
from atlas.atlas.core.server_providers.generic.bmc.driver import BMCDriver
from atlas.metal_server.core.server_power import ServerPower
from atlas.metal_server.doctype.metal_server.metal_server import MetalServer


class TestServerPower(UnitTestCase):
	def setUp(self) -> None:
		super().setUp()
		self.provider = BMCDriver(SimpleNamespace())
		self.server = SimpleNamespace(
			name="record",
			doctype="Metal Server",
			settings=SimpleNamespace(server_provider_controller=self.provider),
			status="Pending",
			reload=Mock(),
			_validate_power_action=Mock(),
			_provider_server_id=Mock(return_value="provider-id"),
			db_set=Mock(),
		)

	def test_refresh_reconciles_lifecycle_without_promoting_readiness(self) -> None:
		for power, current, expected in (
			("On", "Pending", "Pending"),
			("Off", "Running", "Stopped"),
			("On", "Stopped", "Pending"),
			("PoweringOff", "Running", "Pending"),
			("On", "Failed", "Failed"),
		):
			self.server.status = current
			with (
				self.subTest(power=power, current=current),
				patch.object(self.provider, "read_power_status", return_value=ServerPowerStatus(power, None)),
				patch("frappe.cache.lock", return_value=nullcontext()),
				patch("frappe.db.rollback"),
				patch("frappe.db.get_value"),
				patch("frappe.db.commit"),
			):
				ServerPower(self.server).refresh()
			self.assertEqual(self.server.db_set.call_args.args[0], {"status": expected})

	def test_failed_reads_preserve_the_previous_observation(self) -> None:
		with (
			patch.object(self.provider, "read_power_status", side_effect=RedfishError("unreachable")),
			patch("frappe.cache.lock", return_value=nullcontext()),
			patch("frappe.db.rollback"),
			patch("frappe.db.get_value"),
			patch("frappe.db.commit") as commit,
		):
			with self.assertRaises(RedfishError):
				ServerPower(self.server).refresh()
		self.server.db_set.assert_not_called()
		commit.assert_not_called()

	def test_guard_is_rechecked_after_loading_the_saved_record(self) -> None:
		self.server._validate_power_action.side_effect = frappe.PermissionError
		with (
			patch.object(self.provider, "read_power_status") as read,
			patch("frappe.cache.lock", return_value=nullcontext()),
			patch("frappe.db.rollback"),
			patch("frappe.db.get_value"),
		):
			with self.assertRaises(frappe.PermissionError):
				ServerPower(self.server).refresh()
		read.assert_not_called()

	def test_failed_power_on_does_not_save_an_optimistic_state(self) -> None:
		with (
			patch.object(self.provider, "set_power_state", side_effect=RedfishError("outcome unknown")),
			patch.object(self.provider, "read_power_status") as read,
			patch("frappe.cache.lock", return_value=nullcontext()),
			patch("frappe.db.rollback"),
			patch("frappe.db.get_value"),
			patch("frappe.db.commit") as commit,
		):
			with self.assertRaises(RedfishError):
				ServerPower(self.server).set_power_state(ServerPowerAction.START)
		read.assert_not_called()
		self.server.db_set.assert_not_called()
		commit.assert_not_called()

	def test_shutdown_submits_without_reading_or_saving_an_observation(self) -> None:
		with (
			patch.object(self.provider, "set_power_state") as change,
			patch.object(self.provider, "read_power_status") as read,
			patch("frappe.cache.lock", return_value=nullcontext()),
			patch("frappe.db.rollback"),
			patch("frappe.db.get_value"),
			patch("frappe.db.commit") as commit,
		):
			ServerPower(self.server).set_power_state(ServerPowerAction.STOP)
		change.assert_called_once_with("provider-id", ServerPowerAction.STOP)
		read.assert_not_called()
		self.server.db_set.assert_not_called()
		commit.assert_not_called()

	def test_reboot_invalidates_running_readiness(self) -> None:
		self.server.status = "Running"
		with (
			patch.object(self.provider, "set_power_state") as change,
			patch.object(self.provider, "read_power_status", return_value=ServerPowerStatus("On", "OK")),
			patch("frappe.cache.lock", return_value=nullcontext()),
			patch("frappe.db.rollback"),
			patch("frappe.db.get_value"),
			patch("frappe.db.commit"),
		):
			ServerPower(self.server).set_power_state(ServerPowerAction.REBOOT)
		change.assert_called_once_with("provider-id", ServerPowerAction.REBOOT)
		self.assertEqual(self.server.db_set.call_args.args[0]["status"], "Pending")

	def test_power_result_commits_before_releasing_the_server_lock(self) -> None:
		events = []
		lock = Mock()
		lock.__enter__ = Mock(side_effect=lambda: events.append("lock"))
		lock.__exit__ = Mock(side_effect=lambda *_: events.append("unlock"))
		self.server.reload.side_effect = lambda: events.append("reload")
		with (
			patch.object(self.provider, "set_power_state", side_effect=lambda *_: events.append("reset")),
			patch.object(self.provider, "read_power_status", return_value=ServerPowerStatus("On", "OK")),
			patch("frappe.cache.lock", return_value=lock),
			patch("frappe.db.get_value", side_effect=lambda *_args, **_kwargs: events.append("row-lock")),
			patch("frappe.db.rollback", side_effect=lambda: events.append("rollback")),
			patch("frappe.db.commit", side_effect=lambda: events.append("commit")),
		):
			ServerPower(self.server).set_power_state(ServerPowerAction.REBOOT)
		self.assertEqual(
			events,
			["lock", "rollback", "reload", "reset", "rollback", "row-lock", "reload", "commit", "unlock"],
		)

	def test_a_server_deleted_during_the_remote_read_is_not_marked_stopped(self) -> None:
		self.server.status = "Running"
		self.server.setup_job_id = "setup-record"
		self.server._validate_power_action = lambda: MetalServer._validate_power_action(self.server)

		def read_status(_provider_server_id: str) -> ServerPowerStatus:
			self.server.status = "Deleted"
			return ServerPowerStatus("Off", "OK")

		with (
			patch.object(self.provider, "read_power_status", side_effect=read_status),
			patch("frappe.cache.lock", return_value=nullcontext()),
			patch("frappe.db.rollback"),
			patch("frappe.db.get_value"),
			patch("frappe.db.commit") as commit,
			patch("frappe.only_for"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=False),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "deleted"):
				ServerPower(self.server).refresh()
		self.server.db_set.assert_not_called()
		commit.assert_not_called()
