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
		)

		Server.before_validate(server)

		provider.create_server.assert_called_once_with(server)

	def test_validate_checks_the_provider_catalog(self) -> None:
		server = self._server(status="Pending")
		server._validate_provider_catalog = Mock()
		server._sync_disks_if_running = Mock()

		Server.validate(server)

		server._validate_provider_catalog.assert_called_once()

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

	def test_install_metald_queues_the_install_job(self) -> None:
		server = self._server(status="Running")

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.is_job_enqueued", return_value=False),
			patch("atlas.server.doctype.server.server.frappe.enqueue_doc") as enqueue_doc,
		):
			Server.install_metald(server)

		enqueue_doc.assert_called_once_with(
			"Server",
			"node-test-00007",
			"_install_metald",
			queue="long",
			timeout=1200,
			job_id="atlas||server||install-metald||node-test-00007",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	def test_install_metald_rejects_a_missing_download_url(self) -> None:
		server = self._server(status="Running")

		with (
			patch("atlas.server.doctype.server.server.frappe.throw", side_effect=ValueError),
			patch(
				"atlas.server.doctype.server.server.ServerSSHTask.create_for_script_file"
			) as create_for_script_file,
		):
			with self.assertRaises(ValueError):
				Server._install_metald(server)

		create_for_script_file.assert_not_called()

	def test_install_metald_rejects_a_server_that_is_not_running(self) -> None:
		server = self._server(status="Stopped")

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.frappe.throw", side_effect=ValueError),
			patch(
				"atlas.server.doctype.server.server.ServerSSHTask.create_for_script_file"
			) as create_for_script_file,
		):
			with self.assertRaises(ValueError):
				Server.install_metald(server)

		create_for_script_file.assert_not_called()

	def test_install_metald_worker_passes_the_pool_device(self) -> None:
		server = self._server(status="Running")
		server.settings.metald_binary_x86_64_download_url = "https://example.test/metald"
		task = SimpleNamespace(result=SimpleNamespace(is_success=True))

		with patch(
			"atlas.server.doctype.server.server.ServerSSHTask.create_for_script_file", return_value=task
		) as create_for_script_file:
			Server._install_metald(server)

		arguments = create_for_script_file.call_args.kwargs
		self.assertEqual(arguments["script_path"], "install-metald.sh")
		self.assertEqual(
			arguments["environment"],
			{
				"METALD_DOWNLOAD_URL": "https://example.test/metald",
				"STORAGE_POOL_DEVICE": "/dev/md2",
			},
		)

	def test_get_wireguard_ip_address_uses_the_node_number(self) -> None:
		server = self._server(status="Running")

		self.assertEqual(Server._get_wireguard_ip_address(server), "fdab:1::7")

	def test_get_wireguard_ip_address_writes_hexadecimal_fields(self) -> None:
		"""An IPv6 field is hexadecimal, so node 16 is 10 and region 26 is 1a."""
		server = self._server(status="Running")
		server.name = "node-test-00016"
		server.settings.region_id = 26

		self.assertEqual(Server._get_wireguard_ip_address(server), "fdab:1a::10")

	def test_get_wireguard_ip_address_carries_a_large_node_number(self) -> None:
		"""One IPv6 field holds 65535, and the name series runs to 99999."""
		server = self._server(status="Running")
		for node_number, want in (
			("01000", "fdab:1::3e8"),
			("65535", "fdab:1::ffff"),
			("99999", "fdab:1::1:869f"),
		):
			with self.subTest(node_number=node_number):
				server.name = f"node-test-{node_number}"
				self.assertEqual(Server._get_wireguard_ip_address(server), want)

	def test_get_wireguard_ip_address_rejects_an_oversized_region(self) -> None:
		server = self._server(status="Running")
		server.settings.region_id = 0x10000

		with patch("atlas.server.doctype.server.server.frappe.throw", side_effect=ValueError):
			with self.assertRaises(ValueError):
				Server._get_wireguard_ip_address(server)

	def test_get_wireguard_ip_address_rejects_a_name_without_a_node_number(self) -> None:
		server = self._server(status="Running")
		server.name = "node-test-main"

		with patch("atlas.server.doctype.server.server.frappe.throw", side_effect=ValueError):
			with self.assertRaises(ValueError):
				Server._get_wireguard_ip_address(server)

	def test_configure_wireguard_queues_the_job(self) -> None:
		server = self._server(status="Running")

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.is_job_enqueued", return_value=False),
			patch("atlas.server.doctype.server.server.frappe.enqueue_doc") as enqueue_doc,
		):
			Server.configure_wireguard(server)

		self.assertEqual(enqueue_doc.call_args.args[2], "_configure_wireguard")

	def test_configure_wireguard_rejects_a_running_job(self) -> None:
		server = self._server(status="Running")

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.is_job_enqueued", return_value=True),
			patch("atlas.server.doctype.server.server.frappe.throw", side_effect=ValueError),
			patch("atlas.server.doctype.server.server.frappe.enqueue_doc") as enqueue_doc,
		):
			with self.assertRaises(ValueError):
				Server.configure_wireguard(server)

		enqueue_doc.assert_not_called()

	def test_configure_wireguard_job_stores_the_address_and_public_key(self) -> None:
		server = self._server(status="Running")
		output = (
			"==> packages\n==> interface (wg0)\n"
			"===PUBLIC_KEY_START===\nSGVsbG9XaXJlR3VhcmRQdWJsaWNLZXlIZXJlPQ=\n===PUBLIC_KEY_END===\n"
		)
		task = SimpleNamespace(result=SimpleNamespace(output=output, is_success=True))

		with patch(
			"atlas.server.doctype.server.server.ServerSSHTask.create_for_script_file", return_value=task
		) as create_for_script_file:
			Server._configure_wireguard(server)

		arguments = create_for_script_file.call_args.kwargs
		self.assertEqual(
			arguments["environment"],
			{"WIREGUARD_ADDRESS": "fdab:1::7", "WIREGUARD_MTU": 1440},
		)
		self.assertFalse(arguments["run_in_background"])
		server.db_set.assert_any_call("wireguard_ip_address", "fdab:1::7")
		server.db_set.assert_called_with("wireguard_public_key", "SGVsbG9XaXJlR3VhcmRQdWJsaWNLZXlIZXJlPQ=")

	def test_configure_wireguard_job_rejects_output_without_a_public_key(self) -> None:
		"""A successful run that prints no key must not store a marker as the key."""
		server = self._server(status="Running")
		task = SimpleNamespace(result=SimpleNamespace(output="==> packages\n", is_success=True))

		with (
			patch("atlas.server.doctype.server.server.frappe.throw", side_effect=ValueError),
			patch(
				"atlas.server.doctype.server.server.ServerSSHTask.create_for_script_file", return_value=task
			),
		):
			with self.assertRaises(ValueError):
				Server._configure_wireguard(server)

	def test_configure_wireguard_job_rejects_a_failed_run(self) -> None:
		server = self._server(status="Running")
		task = SimpleNamespace(result=SimpleNamespace(output="wg: command not found", is_success=False))

		with (
			patch("atlas.server.doctype.server.server.frappe.throw", side_effect=ValueError),
			patch(
				"atlas.server.doctype.server.server.ServerSSHTask.create_for_script_file", return_value=task
			),
		):
			with self.assertRaises(ValueError):
				Server._configure_wireguard(server)

	def test_configure_wireguard_rejects_a_server_that_is_not_running(self) -> None:
		server = self._server(status="Stopped")

		with (
			patch("atlas.server.doctype.server.server.frappe.only_for"),
			patch("atlas.server.doctype.server.server.frappe.throw", side_effect=ValueError),
			patch(
				"atlas.server.doctype.server.server.ServerSSHTask.create_for_script_file"
			) as create_for_script_file,
		):
			with self.assertRaises(ValueError):
				Server.configure_wireguard(server)

		create_for_script_file.assert_not_called()

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
			doctype="Server",
			name="node-test-00007",
			status=status,
			is_provisioning_completed=False,
			setup_job_id="atlas||server-provision||node-test-00007",
			wireguard_job_id="atlas||server-wireguard||node-test-00007",
			wireguard_ip_address=None,
			settings=SimpleNamespace(
				server_provider_controller=SimpleNamespace(
					archive_server=Mock(),
					get_storage_pool_device=Mock(return_value="/dev/md2"),
				),
				metald_binary_x86_64_download_url=None,
				region_id=1,
				private_network_mtu=1500,
			),
			set=Mock(),
			save=Mock(),
			_enqueue_setup_server=Mock(),
		)

		def db_set(fieldname, value=None, **_options) -> None:
			"""Write the fields on the fake document, as Document.db_set does."""
			values = fieldname if isinstance(fieldname, dict) else {fieldname: value}
			for name, field_value in values.items():
				setattr(server, name, field_value)

		server.db_set = Mock(side_effect=db_set)
		server._parse_disks = MethodType(Server._parse_disks, server)
		server._get_wireguard_ip_address = MethodType(Server._get_wireguard_ip_address, server)
		server._set_wireguard_ip_address_if_not_set = MethodType(
			Server._set_wireguard_ip_address_if_not_set, server
		)
		server._validate_power_action = MethodType(Server._validate_power_action, server)
		return server
