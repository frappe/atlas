from __future__ import annotations

import json
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.server.doctype.server.server import Server


def _disk(device: str) -> dict:
	"""Return one disk from the test RAID layout."""
	return {
		"name": f"/dev/{device}",
		"uuid": None,
		"size": 953 * 1024**3,
		"mountpoint": None,
		"children": [
			{"name": f"/dev/{device}1", "uuid": None, "size": 1024**3 // 2, "mountpoint": None},
			{
				"name": f"/dev/{device}2",
				"uuid": "boot-member-uuid",
				"size": 1024**3,
				"mountpoint": None,
				"children": [
					{"name": "/dev/md0", "uuid": "boot-uuid", "size": 1024**3, "mountpoint": "/boot"}
				],
			},
			{
				"name": f"/dev/{device}3",
				"uuid": "root-member-uuid",
				"size": 64 * 1024**3,
				"mountpoint": None,
				"children": [
					{"name": "/dev/md1", "uuid": "root-uuid", "size": 64 * 1024**3, "mountpoint": "/"}
				],
			},
			{
				"name": f"/dev/{device}4",
				"uuid": "data-member-uuid",
				"size": 888 * 1024**3,
				"mountpoint": None,
				"children": [{"name": "/dev/md2", "uuid": None, "size": 888 * 1024**3, "mountpoint": None}],
			},
		],
	}


_LSBLK_OUTPUT = json.dumps({"blockdevices": [_disk("sda"), _disk("sdb")]})


class TestServer(UnitTestCase):
	def test_before_validate_creates_the_named_server(self) -> None:
		provider = SimpleNamespace(create_server=Mock())
		server = SimpleNamespace(
			provider_server_id=None,
			settings=SimpleNamespace(server_provider_controller=provider),
			_validate_provider_catalog=Mock(),
		)

		Server.before_validate(server)

		server._validate_provider_catalog.assert_called_once()
		provider.create_server.assert_called_once_with(server)

	def test_setup_server_queues_when_no_setup_job_runs(self) -> None:
		server = self._server(status="Failed")

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.is_job_enqueued", return_value=False),
		):
			Server.setup_server(server)

		server.db_set.assert_called_once_with("status", "Pending")
		server._enqueue_setup_server.assert_called_once()

	def test_setup_server_rejects_a_running_setup_job(self) -> None:
		server = self._server(status="Installing")

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.is_job_enqueued", return_value=True),
			patch("atlas.server.doctype.server.server.frappe.throw", side_effect=ValueError),
		):
			with self.assertRaises(ValueError):
				Server.setup_server(server)

	def test_setup_server_skips_a_completed_server(self) -> None:
		server = self._server(status="Running")
		server.is_provisioning_completed = True

		with patch("atlas.server.doctype.server.server.frappe.only_for"):
			Server.setup_server(server)

		server._enqueue_setup_server.assert_not_called()

	def test_ping_server_creates_a_ping_script_log(self) -> None:
		server = self._server(status="Running")
		log = SimpleNamespace(name="SSH-00001")

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch(
				"atlas.server.doctype.server.server.ServerSSHTask.create_for_script_file", return_value=log
			) as create_for_script_file,
		):
			log_name = Server.ping_server(server)

		self.assertEqual(log_name, "SSH-00001")
		create_for_script_file.assert_called_once_with(server=server.name, script_path="ping-server.sh")

	def test_ping_server_rejects_a_server_that_is_not_running(self) -> None:
		server = self._server(status="Stopped")

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.frappe.throw", side_effect=ValueError),
			patch(
				"atlas.server.doctype.server.server.ServerSSHTask.create_for_script_file"
			) as create_for_script_file,
		):
			with self.assertRaises(ValueError):
				Server.ping_server(server)

		create_for_script_file.assert_not_called()

	def test_sync_disks_stores_the_mounted_devices_and_the_storage_pool(self) -> None:
		server = self._server(status="Running")
		task = SimpleNamespace(result=SimpleNamespace(output=_LSBLK_OUTPUT, is_success=True))

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch(
				"atlas.server.doctype.server.server.ServerSSHTask.create_for_command", return_value=task
			) as create_for_command,
		):
			Server.sync_disks(server)

		self.assertFalse(create_for_command.call_args.kwargs["run_in_background"])
		server.set.assert_called_once_with(
			"disks",
			[
				{
					"device": "/dev/md0",
					"uuid": "boot-uuid",
					"mount_point": "/boot",
					"size_gb": "1.00",
				},
				{"device": "/dev/md1", "uuid": "root-uuid", "mount_point": "/", "size_gb": "64.00"},
				{"device": "/dev/md2", "uuid": "", "mount_point": "", "size_gb": "888.00"},
			],
		)
		server.save.assert_called_once()

	def test_sync_disks_reports_a_raid_array_once_for_both_members(self) -> None:
		server = self._server(status="Running")
		task = SimpleNamespace(result=SimpleNamespace(output=_LSBLK_OUTPUT, is_success=True))

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.ServerSSHTask.create_for_command", return_value=task),
		):
			Server.sync_disks(server)

		devices = [disk["device"] for disk in server.set.call_args.args[1]]
		self.assertEqual(len(devices), len(set(devices)))

	def test_sync_disks_rejects_a_failed_lsblk_run(self) -> None:
		server = self._server(status="Running")
		task = SimpleNamespace(result=SimpleNamespace(output="lsblk: not found", is_success=False))

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.frappe.throw", side_effect=ValueError),
			patch("atlas.server.doctype.server.server.ServerSSHTask.create_for_command", return_value=task),
		):
			with self.assertRaises(ValueError):
				Server.sync_disks(server)

		server.save.assert_not_called()

	def test_sync_disks_rejects_a_server_that_is_not_running(self) -> None:
		server = self._server(status="Stopped")

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.frappe.throw", side_effect=ValueError),
			patch(
				"atlas.server.doctype.server.server.ServerSSHTask.create_for_command"
			) as create_for_command,
		):
			with self.assertRaises(ValueError):
				Server.sync_disks(server)

		create_for_command.assert_not_called()

	def test_poweroff_server_marks_the_server_stopped(self) -> None:
		server = self._server(status="Running")
		server.settings.server_provider_controller.poweroff_server = Mock()

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.is_job_enqueued", return_value=False),
		):
			Server.poweroff_server(server)

		server.settings.server_provider_controller.poweroff_server.assert_called_once_with(server)
		server.db_set.assert_called_once_with("status", "Stopped")

	def test_poweron_server_marks_a_provisioned_server_running(self) -> None:
		server = self._server(status="Stopped")
		server.is_provisioning_completed = True
		server.settings.server_provider_controller.poweron_server = Mock()

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.is_job_enqueued", return_value=False),
		):
			Server.poweron_server(server)

		server.db_set.assert_called_once_with("status", "Running")

	def test_poweron_server_keeps_the_status_while_provisioning(self) -> None:
		server = self._server(status="Failed")
		server.settings.server_provider_controller.poweron_server = Mock()

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.is_job_enqueued", return_value=False),
		):
			Server.poweron_server(server)

		server.settings.server_provider_controller.poweron_server.assert_called_once_with(server)
		server.db_set.assert_not_called()

	def test_reboot_server_keeps_the_status(self) -> None:
		server = self._server(status="Running")
		server.settings.server_provider_controller.reboot_server = Mock()

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.is_job_enqueued", return_value=False),
		):
			Server.reboot_server(server)

		server.settings.server_provider_controller.reboot_server.assert_called_once_with(server)
		server.db_set.assert_not_called()

	def test_power_action_rejects_a_deleted_server(self) -> None:
		server = self._server(status="Deleted")

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.frappe.throw", side_effect=ValueError),
		):
			with self.assertRaises(ValueError):
				Server.reboot_server(server)

	def test_power_action_rejects_a_running_setup_job(self) -> None:
		server = self._server(status="Installing")

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.is_job_enqueued", return_value=True),
			patch("atlas.server.doctype.server.server.frappe.throw", side_effect=ValueError),
		):
			with self.assertRaises(ValueError):
				Server.poweroff_server(server)

	def test_archive_server_deletes_the_provider_server(self) -> None:
		server = self._server(status="Failed")

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.is_job_enqueued", return_value=False),
		):
			Server.archive_server(server)

		server.settings.server_provider_controller.archive_server.assert_called_once_with(server)
		server.db_set.assert_called_once_with({"status": "Deleted", "is_provisioning_completed": 0})

	def test_archive_server_skips_a_deleted_server(self) -> None:
		server = self._server(status="Deleted")

		with patch("atlas.server.doctype.server.server.frappe.only_for"):
			Server.archive_server(server)

		server.settings.server_provider_controller.archive_server.assert_not_called()
		server.db_set.assert_not_called()

	def test_archive_server_rejects_a_running_setup_job(self) -> None:
		server = self._server(status="Installing")

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.is_job_enqueued", return_value=True),
			patch("atlas.server.doctype.server.server.frappe.throw", side_effect=ValueError),
		):
			with self.assertRaises(ValueError):
				Server.archive_server(server)

	@staticmethod
	def _server(*, status: str) -> SimpleNamespace:
		server = SimpleNamespace(
			name="node-test-00001",
			status=status,
			is_provisioning_completed=False,
			setup_job_id="atlas||server-provision||node-test-00001",
			settings=SimpleNamespace(
				server_provider_controller=SimpleNamespace(
					archive_server=Mock(),
					get_storage_pool_device=Mock(return_value="/dev/md2"),
				)
			),
			db_set=Mock(),
			set=Mock(),
			save=Mock(),
			_enqueue_setup_server=Mock(),
		)
		server._parse_disks = MethodType(Server._parse_disks, server)
		return server
