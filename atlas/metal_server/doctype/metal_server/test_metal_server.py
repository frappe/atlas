from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.base import ProviderServer, ServerPowerAction, ServerProvider
from atlas.atlas.core.server_providers.generic import RedfishError
from atlas.atlas.core.tls.metal import CERTIFICATE_RENEWAL_WINDOW_DAYS
from atlas.metal_server.doctype.metal_server.metal_server import (
	MetalServer,
	renew_expiring_tls_certificates,
)


def _disk(device: str) -> dict:
	"""Return one disk from the test RAID layout."""
	return {
		"name": f"/dev/{device}",
		"type": "disk",
		"uuid": None,
		"size": 953 * 1024**3,
		"mountpoint": None,
		"children": [
			{
				"name": f"/dev/{device}1",
				"type": "part",
				"uuid": None,
				"size": 1024**3 // 2,
				"mountpoint": None,
			},
			{
				"name": f"/dev/{device}2",
				"type": "part",
				"uuid": "boot-member-uuid",
				"size": 1024**3,
				"mountpoint": None,
				"children": [
					{
						"name": "/dev/md0",
						"type": "raid1",
						"uuid": "boot-uuid",
						"size": 1024**3,
						"mountpoint": "/boot",
					}
				],
			},
			{
				"name": f"/dev/{device}3",
				"type": "part",
				"uuid": "root-member-uuid",
				"size": 64 * 1024**3,
				"mountpoint": None,
				"children": [
					{
						"name": "/dev/md1",
						"type": "raid1",
						"uuid": "root-uuid",
						"size": 64 * 1024**3,
						"mountpoint": "/",
					}
				],
			},
			{
				"name": f"/dev/{device}4",
				"type": "part",
				"uuid": "data-member-uuid",
				"size": 888 * 1024**3,
				"mountpoint": None,
				"children": [
					{
						"name": "/dev/md2",
						"type": "raid1",
						"uuid": None,
						"size": 888 * 1024**3,
						"mountpoint": None,
					}
				],
			},
		],
	}


_LSBLK_OUTPUT = json.dumps({"blockdevices": [_disk("sda"), _disk("sdb")]})

SERVER_NAME = "01a0c05f-e209-70ad-a183-dda2a727cd8b"
# The low 96 bits of SERVER_NAME under region 1.
SERVER_MESH_ADDRESS = "fdab:1:e209:70ad:a183:dda2:a727:cd8b"


class TestServer(UnitTestCase):
	def test_before_validate_takes_the_architecture_without_provider_creation(self) -> None:
		provider = SimpleNamespace(validate_settings=Mock(), validate_server=Mock(), ensure_server=Mock())
		server = SimpleNamespace(
			name=SERVER_NAME,
			provider_server_id=None,
			architecture=None,
			server_size="large",
			server_image="Ubuntu_26.04",
			status="Pending",
			settings=SimpleNamespace(server_provider_controller=provider),
			_validate_provider_catalog=Mock(),
		)

		with patch(
			"atlas.metal_server.doctype.metal_server.metal_server.frappe.get_doc",
			return_value=SimpleNamespace(architecture="amd64"),
		):
			MetalServer.before_validate(server)

		provider.validate_settings.assert_called_once_with()
		provider.validate_server.assert_called_once_with(server)
		self.assertEqual(server.architecture, "amd64")
		provider.ensure_server.assert_not_called()

	def test_validate_trims_the_tags_and_fills_the_mesh_address(self) -> None:
		tags = [SimpleNamespace(key=" env ", value=" local ")]
		server = SimpleNamespace(
			get=lambda fieldname: tags,
			_validate_title=Mock(),
			_validate_redfish=Mock(),
			_set_wireguard_ip_address_if_not_set=Mock(),
		)

		MetalServer.validate(server)

		self.assertEqual((tags[0].key, tags[0].value), ("env", "local"))
		server._set_wireguard_ip_address_if_not_set.assert_called_once_with()

	def test_validate_rejects_a_repeated_tag_key(self) -> None:
		tags = [SimpleNamespace(key="env", value="local"), SimpleNamespace(key="env", value="dev")]
		server = SimpleNamespace(
			get=lambda fieldname: tags,
			_validate_title=Mock(),
			_validate_redfish=Mock(),
			_set_wireguard_ip_address_if_not_set=Mock(),
		)

		with self.assertRaises(frappe.ValidationError):
			MetalServer.validate(server)

		server._set_wireguard_ip_address_if_not_set.assert_not_called()

	def test_validate_redfish_requires_the_url_username_and_password_together(self) -> None:
		MetalServer._validate_redfish(
			SimpleNamespace(redfish_url=None, redfish_username=None, redfish_password=None)
		)
		MetalServer._validate_redfish(
			SimpleNamespace(
				redfish_url="https://bmc.local", redfish_username="admin", redfish_password="secret"
			)
		)

		with self.assertRaises(frappe.ValidationError):
			MetalServer._validate_redfish(
				SimpleNamespace(
					redfish_url="https://bmc.local", redfish_username="admin", redfish_password=None
				)
			)

	def test_redfish_power_state_skips_the_bmc_when_access_is_off(self) -> None:
		server = SimpleNamespace(is_bmc_access_enabled=False, redfish_url="https://bmc.local")

		with patch("atlas.metal_server.doctype.metal_server.metal_server.RedfishClient") as client:
			self.assertIsNone(MetalServer.redfish_power_state.fget(server))

		client.assert_not_called()

	def test_redfish_power_state_shows_a_failed_read_in_the_form(self) -> None:
		server = SimpleNamespace(
			is_bmc_access_enabled=True,
			redfish_url="https://bmc.local",
			redfish_username="admin",
			get_password=Mock(return_value="secret"),
			set_onload=Mock(),
		)

		with patch("atlas.metal_server.doctype.metal_server.metal_server.RedfishClient") as client:
			client.return_value.read_power_state.side_effect = RedfishError("status 401")
			self.assertIsNone(MetalServer.redfish_power_state.fget(server))

		server.set_onload.assert_called_once_with("redfish_power_error", "status 401")

	def test_ensure_provider_server_identifies_the_host_by_name(self) -> None:
		provider = SimpleNamespace(
			ensure_server=Mock(
				return_value=ProviderServer(
					provider_server_id="server-id",
					status="Installing",
					public_ipv4_address="203.0.113.1",
					provider_metadata={"server": {"id": "server-id"}},
				)
			)
		)
		server = SimpleNamespace(
			name=SERVER_NAME,
			provider_server_id=None,
			server_size="large",
			server_image="Ubuntu_26.04",
			status="Pending",
			settings=SimpleNamespace(server_provider_controller=provider),
			_provider_metadata=MetalServer._provider_metadata,
		)

		with patch(
			"atlas.metal_server.doctype.metal_server.metal_server.frappe.get_doc",
			return_value=SimpleNamespace(provider_metadata="{}"),
		):
			MetalServer.ensure_provider_server(server)

		self.assertEqual(provider.ensure_server.call_args.args[0].name, SERVER_NAME)
		self.assertEqual(server.provider_server_id, "server-id")

	def test_provisioning_worker_runs_as_administrator(self) -> None:
		previous_user = frappe.session.user
		seen_users: list[str] = []
		frappe.set_user("Guest")
		try:
			with patch(
				"atlas.metal_server.doctype.metal_server.metal_server.ServerProvisioner"
			) as provisioner:
				provisioner.return_value.run.side_effect = lambda: seen_users.append(frappe.session.user)
				MetalServer._setup_server(SimpleNamespace())
		finally:
			frappe.set_user(previous_user)

		self.assertEqual(seen_users, ["Administrator"])

	def test_setup_server_queues_when_no_setup_job_runs(self) -> None:
		server = self._server(status="Failed")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=False),
		):
			MetalServer.setup_server(server)

		server.db_set.assert_called_once_with("status", "Pending")
		server._enqueue_setup_server.assert_called_once()

	def test_setup_server_rejects_a_running_setup_job(self) -> None:
		server = self._server(status="Installing")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=True),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
		):
			with self.assertRaises(ValueError):
				MetalServer.setup_server(server)

	def test_setup_server_skips_a_completed_server(self) -> None:
		server = self._server(status="Running")
		server.is_provisioning_completed = True

		with patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"):
			MetalServer.setup_server(server)

		server._enqueue_setup_server.assert_not_called()

	def test_ping_server_rejects_a_server_that_is_not_running(self) -> None:
		server = self._server(status="Stopped")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.SSHTask.create_for_script_file"
			) as create_for_script_file,
		):
			with self.assertRaises(ValueError):
				MetalServer.ping_server(server)

		create_for_script_file.assert_not_called()

	def test_sync_disks_stores_the_mounted_devices_and_the_storage_pool(self) -> None:
		server = self._server(status="Running")
		task = SimpleNamespace(result=SimpleNamespace(output=_LSBLK_OUTPUT, is_success=True))

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch(
				"atlas.metal_server.core.disk_inventory.SSHTask.create_for_command",
				return_value=task,
			) as create_for_command,
		):
			MetalServer._sync_disks(server)

		self.assertFalse(create_for_command.call_args.kwargs["run_in_background"])
		server.set.assert_called_once_with(
			"disks",
			[
				{"device": "/dev/sda", "uuid": "", "mount_point": "", "size_gb": "953.00"},
				{
					"device": "/dev/md0",
					"uuid": "boot-uuid",
					"mount_point": "/boot",
					"size_gb": "1.00",
				},
				{"device": "/dev/md1", "uuid": "root-uuid", "mount_point": "/", "size_gb": "64.00"},
				{"device": "/dev/md2", "uuid": "", "mount_point": "", "size_gb": "888.00"},
				{"device": "/dev/sdb", "uuid": "", "mount_point": "", "size_gb": "953.00"},
			],
		)
		server.save.assert_called_once()

	def test_sync_disks_reports_a_raid_array_once_for_both_members(self) -> None:
		server = self._server(status="Running")
		task = SimpleNamespace(result=SimpleNamespace(output=_LSBLK_OUTPUT, is_success=True))

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch(
				"atlas.metal_server.core.disk_inventory.SSHTask.create_for_command",
				return_value=task,
			),
		):
			MetalServer._sync_disks(server)

		devices = [disk["device"] for disk in server.set.call_args.args[1]]
		self.assertEqual(len(devices), len(set(devices)))

	def test_sync_disks_reports_every_whole_disk_and_skips_loop_devices(self) -> None:
		server = self._server(status="Running")
		output = json.dumps(
			{
				"blockdevices": [
					{"name": "/dev/loop0", "type": "loop", "size": 28 * 1024**2, "mountpoint": "/snap/core"},
					{
						"name": "/dev/nvme1n1",
						"type": "disk",
						"uuid": None,
						"size": 64 * 1024**3,
						"mountpoint": None,
						"children": [
							{
								"name": "/dev/nvme1n1p1",
								"type": "part",
								"uuid": "root-uuid",
								"size": 62 * 1024**3,
								"mountpoint": "/",
							},
							{
								"name": "/dev/nvme1n1p14",
								"type": "part",
								"uuid": None,
								"size": 4 * 1024**2,
								"mountpoint": None,
							},
						],
					},
					{
						"name": "/dev/nvme0n1",
						"type": "disk",
						"uuid": None,
						"size": 500 * 1024**3,
						"mountpoint": None,
						"children": [
							{
								"name": "/dev/nvme0n1p1",
								"type": "part",
								"uuid": None,
								"size": 500 * 1024**3,
								"mountpoint": None,
							}
						],
					},
				]
			}
		)
		task = SimpleNamespace(result=SimpleNamespace(output=output, is_success=True))

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch(
				"atlas.metal_server.core.disk_inventory.SSHTask.create_for_command",
				return_value=task,
			),
		):
			MetalServer._sync_disks(server)

		self.assertEqual(
			[disk["device"] for disk in server.set.call_args.args[1]],
			["/dev/nvme1n1", "/dev/nvme1n1p1", "/dev/nvme0n1"],
		)

	def test_sync_disks_rejects_a_failed_lsblk_run(self) -> None:
		server = self._server(status="Running")
		task = SimpleNamespace(result=SimpleNamespace(output="lsblk: not found", is_success=False))

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
			patch(
				"atlas.metal_server.core.disk_inventory.SSHTask.create_for_command",
				return_value=task,
			),
		):
			with self.assertRaises(ValueError):
				MetalServer._sync_disks(server)

		server.save.assert_not_called()

	def test_sync_disks_queues_one_job_per_server(self) -> None:
		server = self._server(status="Running")
		server.enqueue_disk_sync = lambda: MetalServer.enqueue_disk_sync(server)

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.enqueue_doc") as enqueue_doc,
			patch("atlas.metal_server.core.disk_inventory.SSHTask.create_for_command") as create_for_command,
		):
			MetalServer.sync_disks(server)

		create_for_command.assert_not_called()
		self.assertEqual(enqueue_doc.call_args.args, ("Metal Server", SERVER_NAME, "_sync_disks"))
		self.assertEqual(enqueue_doc.call_args.kwargs["job_id"], f"atlas||server||sync-disks||{SERVER_NAME}")
		self.assertTrue(enqueue_doc.call_args.kwargs["deduplicate"])

	def test_sync_disks_rejects_a_server_that_is_not_running(self) -> None:
		server = self._server(status="Stopped")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
			patch("atlas.metal_server.core.disk_inventory.SSHTask.create_for_command") as create_for_command,
		):
			with self.assertRaises(ValueError):
				MetalServer.sync_disks(server)

		create_for_command.assert_not_called()

	def test_resize_volume_queues_growth_without_waiting_for_commit(self) -> None:
		server = self._server(status="Running")
		volumes = Mock()
		server.settings.server_provider = "AWS"
		server._aws_volumes = Mock(return_value=volumes)

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.enqueue_doc") as enqueue_doc,
		):
			MetalServer.resize_volume(server, "storage", 600, 3000, 250)

		volumes.modify.assert_called_once_with(server, "storage", 600, 3000, 250)
		self.assertFalse(enqueue_doc.call_args.kwargs["enqueue_after_commit"])

	# An unprovisioned host has no Metal token.
	def test_sync_state_rejects_a_server_that_is_not_ready(self) -> None:
		server = self._server(status="Running")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.enqueue_server_sync"
			) as enqueue_server_sync,
		):
			with self.assertRaises(ValueError):
				MetalServer.sync_state(server)

		enqueue_server_sync.assert_not_called()

	def test_every_metald_operation_shares_one_job_lock(self) -> None:
		server = self._server(status="Running")
		job_ids = []
		timeouts = []

		for operation in (
			MetalServer.install_metald,
			MetalServer.upgrade_metald,
			MetalServer.enqueue_tls_certificate_renewal,
		):
			with (
				patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
				patch(
					"atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued",
					return_value=False,
				),
				patch(
					"atlas.metal_server.doctype.metal_server.metal_server.frappe.enqueue_doc"
				) as enqueue_doc,
			):
				operation(server)

			job_ids.append(enqueue_doc.call_args.kwargs["job_id"])
			timeouts.append(enqueue_doc.call_args.kwargs["timeout"])

		self.assertEqual(job_ids, [server.metald_job_id] * 3)
		self.assertEqual(timeouts, [3600, 1200, 1200])

	def test_install_metald_rejects_a_missing_binary(self) -> None:
		server = self._server(status="Running")

		with (
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
			patch(
				"atlas.metal_server.core.host_installation.SSHTask.create_for_script_file"
			) as create_for_script_file,
		):
			with self.assertRaises(ValueError):
				MetalServer._install_metald(server)

		create_for_script_file.assert_not_called()

	def test_install_metald_rejects_a_server_that_is_not_running(self) -> None:
		server = self._server(status="Stopped")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
			patch(
				"atlas.metal_server.core.host_installation.SSHTask.create_for_script_file"
			) as create_for_script_file,
		):
			with self.assertRaises(ValueError):
				MetalServer.install_metald(server)

		create_for_script_file.assert_not_called()

	def test_install_metald_worker_passes_the_pool_device(self) -> None:
		server = self._server(status="Running")
		server.settings.metald_binary_x86_64_file = "metald-file"
		server.wireguard_ip_address = "fdab:1::7"
		task = SimpleNamespace(result=SimpleNamespace(is_success=True))
		tls_result = SimpleNamespace(is_success=True)
		file_urls = {
			"metald-file": "https://atlas.test/files/metald-linux-amd64",
			"wg-mesh-file": "https://atlas.test/files/atlas-wg-mesh-linux-amd64",
		}

		with (
			patch(
				"atlas.metal_server.core.host_installation.get_download_url",
				side_effect=lambda file_name: file_urls[file_name],
			),
			patch(
				"atlas.metal_server.core.host_installation.SSHTask.create_for_script_file",
				return_value=task,
			) as create_for_script_file,
			patch(
				"atlas.metal_server.core.host_installation.ensure_server_certificate",
				return_value=("ca", "certificate", "private-key"),
			),
			patch("atlas.metal_server.core.host_installation.SSHRunner") as ssh_runner,
		):
			ssh_runner.return_value.run_script.return_value = tls_result
			MetalServer._install_metald(server)

		storage_arguments, arguments, firewall_arguments = (
			call.kwargs for call in create_for_script_file.call_args_list
		)
		self.assertEqual(storage_arguments["script_path"], "install-metal-storage.sh")
		self.assertEqual(storage_arguments["environment"], {"STORAGE_POOL_DEVICE": "/dev/md2"})
		self.assertEqual(firewall_arguments["script_path"], "install-host-firewall.sh")
		self.assertEqual(
			firewall_arguments["environment"],
			{
				"ATLAS_WIREGUARD_ADDRESS": "fdaa:1::ffff:ffff:ffff:ffff",
				"MESH_UPLINK_INTERFACE": "eno1.1878",
				"PRIVATE_NETWORK_CIDR": "10.0.0.0/20",
				"WIREGUARD_LISTEN_PORT": 51820,
			},
		)
		self.assertEqual(arguments["script_path"], "install-metald.sh")
		self.assertEqual(
			arguments["environment"],
			{
				"METALD_DOWNLOAD_URL": "https://atlas.test/files/metald-linux-amd64",
				"METALD_SHA256": "metald-binary-sha256",
				"WG_MESH_DOWNLOAD_URL": "https://atlas.test/files/atlas-wg-mesh-linux-amd64",
				"WG_MESH_SHA256": "wg-mesh-binary-sha256",
				"LISTEN_ADDRESS": "[fdab:1::7]:9000",
				"ATLAS_COMMON_NAME": "atlas.example.test",
				"COORDINATION_LISTEN_ADDRESS": "[fdab:1::7]:9001",
				"MESH_UPLINK_INTERFACE": "eno1.1878",
				"PRIVATE_NETWORK_CIDR": "10.0.0.0/20",
				"ATLAS_MESH_ADDRESS": "fdaa:1::ffff:ffff:ffff:ffff",
			},
		)
		ssh_runner.return_value.run_script.assert_called_once_with(
			"install-metal-tls.sh",
			data={
				"METAL_TLS_CA_CERTIFICATE": "ca",
				"METAL_TLS_CERTIFICATE": "certificate",
				"METAL_TLS_PRIVATE_KEY": "private-key",
			},
			timeout_seconds=1200,
		)

	def test_tls_renewal_waits_for_a_running_metald_job(self) -> None:
		server = self._server(status="Running")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=True),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.enqueue_doc") as enqueue_doc,
		):
			with self.assertRaises(ValueError):
				MetalServer.enqueue_tls_certificate_renewal(server)

		enqueue_doc.assert_not_called()

	def test_renew_tls_certificate_rejects_a_server_that_is_not_provisioned(self) -> None:
		server = self._server(status="Running")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.enqueue_doc") as enqueue_doc,
		):
			with self.assertRaises(ValueError):
				MetalServer.renew_tls_certificate(server)

		enqueue_doc.assert_not_called()

	def test_upgrade_metald_waits_for_a_running_metald_job(self) -> None:
		"""Install and upgrade share one lock, so they never restart metald together."""
		server = self._server(status="Running")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=True),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.enqueue_doc") as enqueue_doc,
		):
			with self.assertRaises(ValueError):
				MetalServer.upgrade_metald(server)

		enqueue_doc.assert_not_called()

	def test_upgrade_metald_rejects_a_server_that_is_not_running(self) -> None:
		server = self._server(status="Stopped")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
			patch(
				"atlas.metal_server.core.host_installation.SSHTask.create_for_script_file"
			) as create_for_script_file,
		):
			with self.assertRaises(ValueError):
				MetalServer.upgrade_metald(server)

		create_for_script_file.assert_not_called()

	def test_upgrade_metald_rejects_a_missing_binary(self) -> None:
		server = self._server(status="Running")

		with (
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
			patch(
				"atlas.metal_server.core.host_installation.SSHTask.create_for_script_file"
			) as create_for_script_file,
		):
			with self.assertRaises(ValueError):
				MetalServer._upgrade_metald(server)

		create_for_script_file.assert_not_called()

	def test_upgrade_metald_worker_sends_only_the_download_url_and_digest(self) -> None:
		"""The upgrade replaces the binary. It does not rewrite host configuration."""
		server = self._server(status="Running")
		server.settings.metald_binary_x86_64_file = "metald-file"
		task = SimpleNamespace(result=SimpleNamespace(is_success=True))

		with (
			patch(
				"atlas.metal_server.core.host_installation.get_download_url",
				return_value="https://atlas.test/files/metald-linux-amd64",
			),
			patch(
				"atlas.metal_server.core.host_installation.SSHTask.create_for_script_file",
				return_value=task,
			) as create_for_script_file,
		):
			MetalServer._upgrade_metald(server)

		arguments = create_for_script_file.call_args.kwargs
		self.assertEqual(arguments["script_path"], "upgrade-metald.sh")
		self.assertEqual(
			arguments["environment"],
			{
				"METALD_DOWNLOAD_URL": "https://atlas.test/files/metald-linux-amd64",
				"METALD_SHA256": "metald-binary-sha256",
			},
		)

	def test_upgrade_metald_reports_the_reason_the_script_printed(self) -> None:
		server = self._server(status="Running")
		server.settings.metald_binary_x86_64_file = "metald-file"
		task = SimpleNamespace(
			result=SimpleNamespace(
				is_success=False,
				exit_code=1,
				output="==> restart metal.service\n    metal.service did not start; restoring fd6a6eb\n",
			)
		)

		with (
			patch(
				"atlas.metal_server.core.host_installation.get_download_url",
				return_value="https://atlas.test/files/metald-linux-amd64",
			),
			patch(
				"atlas.metal_server.core.host_installation.SSHTask.create_for_script_file",
				return_value=task,
			),
			self.assertRaises(frappe.ValidationError) as raised,
		):
			MetalServer._upgrade_metald(server)

		message = str(raised.exception)
		self.assertIn("Exit code 1", message)
		self.assertIn("metal.service did not start", message)

	def test_a_script_failure_reports_the_last_printed_lines(self) -> None:
		from atlas.metal_server.core.host_installation import get_failure_reason

		reason = get_failure_reason(
			"==> download metald\n\ncurl: (22) The requested URL returned error: 404\n"
		)

		self.assertEqual(reason, "==> download metald curl: (22) The requested URL returned error: 404")

	def test_a_script_that_printed_nothing_is_reported(self) -> None:
		from atlas.metal_server.core.host_installation import get_failure_reason

		self.assertIn("no output", get_failure_reason(""))

	def test_a_script_that_did_not_run_is_reported(self) -> None:
		from atlas.metal_server.core.host_installation import throw_script_failure

		with self.assertRaises(frappe.ValidationError) as raised:
			throw_script_failure(f"Could not upgrade metald on server {SERVER_NAME}.", None)

		self.assertIn("did not run", str(raised.exception))

	def test_install_metald_worker_needs_a_private_network_interface(self) -> None:
		"""Atlas WG Mesh hooks this interface, so metald cannot guess it."""
		server = self._server(status="Running")
		server.settings.metald_binary_x86_64_file = "metald-file"
		server.private_network_interface = None

		with (
			patch(
				"atlas.metal_server.core.host_installation.SSHTask.create_for_script_file"
			) as create_for_script_file,
			self.assertRaises(frappe.ValidationError),
		):
			MetalServer._install_metald(server)

		create_for_script_file.assert_not_called()

	def test_import_continues_the_setup_of_a_known_server(self) -> None:
		"""Importing the same provider server again must not create a second record."""
		server = self._server(status="Failed")
		server._enqueue_setup_server = Mock()
		server._set_storage_pool_device = MethodType(MetalServer._set_storage_pool_device, server)

		with (
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.db.advisory_lock",
				return_value=nullcontext(),
			),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.db.rollback"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.db.commit") as commit,
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.db.get_value",
				return_value=SERVER_NAME,
			),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.get_doc", return_value=server),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.new_doc") as new_doc,
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=False),
		):
			MetalServer.import_from_provider("server-id", "/root/disks/atlas.img")

		new_doc.assert_not_called()
		commit.assert_called_once_with()
		server._enqueue_setup_server.assert_called_once_with()
		self.assertEqual(server.status, "Pending")
		self.assertEqual(json.loads(server.provider_metadata)["storage_pool_device"], "/root/disks/atlas.img")

	def test_import_inserts_a_pending_server_from_the_provider(self) -> None:
		server = self._server(status="Pending")
		server.provider_server_id = None
		server.insert = Mock()
		server._set_storage_pool_device = MethodType(MetalServer._set_storage_pool_device, server)
		provider = server.settings.server_provider_controller
		provider.import_server = Mock(
			side_effect=lambda imported: setattr(imported, "server_size", "EM-A116X-SSD")
		)

		with (
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.db.advisory_lock",
				return_value=nullcontext(),
			),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.db.rollback"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.db.commit") as commit,
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.db.get_value",
				side_effect=[None, "amd64"],
			),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.new_doc", return_value=server),
		):
			MetalServer.import_from_provider("server-id")

		provider.import_server.assert_called_once_with(server)
		self.assertEqual(
			(server.provider_server_id, server.architecture, server.status), ("server-id", "amd64", "Pending")
		)
		server.insert.assert_called_once_with(ignore_permissions=True)
		commit.assert_called_once_with()

	def test_an_imported_storage_device_replaces_the_provider_device(self) -> None:
		server = self._server(status="Running")
		server.provider_metadata = json.dumps({"storage_pool_device": "/root/disks/atlas.img"})

		provider = SimpleNamespace(storage_pool_device=Mock(return_value="/dev/md2"))
		self.assertEqual(ServerProvider.get_storage_pool_device(provider, server), "/root/disks/atlas.img")

		server.provider_metadata = "{}"
		self.assertEqual(ServerProvider.get_storage_pool_device(provider, server), "/dev/md2")

	def test_a_new_host_title_is_one_dns_label(self) -> None:
		server = SimpleNamespace(name=SERVER_NAME, title="par-1-host-3", is_new=lambda: True)
		MetalServer._validate_title(server)

		for title in ("Par 1", "host:1", "-host", ""):
			server.title = title
			with self.assertRaises(frappe.ValidationError):
				MetalServer._validate_title(server)

	def test_a_new_host_gets_a_region_title(self) -> None:
		server = SimpleNamespace(title="typed-title", settings=SimpleNamespace(region_name="waw-3"))
		with patch(
			"atlas.metal_server.doctype.metal_server.metal_server.make_autoname", return_value="metal-waw-3-4"
		) as make_autoname:
			MetalServer.before_insert(server)

		make_autoname.assert_called_once_with("metal-waw-3-.#", doc=server)
		self.assertEqual(server.title, "metal-waw-3-4")

	def test_a_renamed_host_title_is_checked_and_an_unchanged_one_is_not(self) -> None:
		server = SimpleNamespace(
			name=SERVER_NAME, title="Renamed Host", is_new=lambda: False, has_value_changed=lambda field: True
		)
		with self.assertRaises(frappe.ValidationError):
			MetalServer._validate_title(server)

		server.has_value_changed = lambda field: False
		MetalServer._validate_title(server)

	def test_ssh_uses_wg0_once_the_host_has_a_wireguard_key(self) -> None:
		server = self._server(status="Running")
		server.wireguard_ip_address = "fdab:1::7"

		server.wireguard_public_key = None
		self.assertEqual(MetalServer.ssh_host.fget(server), "203.0.113.7")

		server.wireguard_public_key = "host-key"
		self.assertEqual(MetalServer.ssh_host.fget(server), "fdab:1::7")

	def test_get_wireguard_ip_address_uses_the_server_uuid(self) -> None:
		server = self._server(status="Running")

		self.assertEqual(MetalServer._get_wireguard_ip_address(server), SERVER_MESH_ADDRESS)

	def test_get_wireguard_ip_address_writes_the_region_in_hexadecimal(self) -> None:
		"""An IPv6 field is hexadecimal, so region 26 is 1a."""
		server = self._server(status="Running")
		server.settings.region_id = 26

		self.assertEqual(
			MetalServer._get_wireguard_ip_address(server), "fdab:1a:e209:70ad:a183:dda2:a727:cd8b"
		)

	def test_get_wireguard_ip_address_keeps_the_host_inside_96_bits(self) -> None:
		"""Only the low 96 bits of the UUID fit, so the region field stays intact."""
		server = self._server(status="Running")
		for name, want in (
			("00000000-0000-0000-0000-000000000000", "fdab:1::"),
			("ffffffff-ffff-ffff-ffff-ffffffffffff", "fdab:1:ffff:ffff:ffff:ffff:ffff:ffff"),
		):
			with self.subTest(name=name):
				server.name = name
				self.assertEqual(MetalServer._get_wireguard_ip_address(server), want)

	def test_get_wireguard_ip_address_rejects_an_oversized_region(self) -> None:
		server = self._server(status="Running")
		server.settings.region_id = 0x10000

		with patch(
			"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
		):
			with self.assertRaises(ValueError):
				MetalServer._get_wireguard_ip_address(server)

	def test_configure_wireguard_rejects_a_running_job(self) -> None:
		server = self._server(status="Running")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=True),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.enqueue_doc") as enqueue_doc,
		):
			with self.assertRaises(ValueError):
				MetalServer.configure_wireguard(server)

		enqueue_doc.assert_not_called()

	def test_configure_wireguard_job_stores_the_address_and_public_key(self) -> None:
		server = self._server(status="Running")
		output = (
			"==> packages\n==> interface (wg0)\n"
			"===PUBLIC_KEY_START===\nC9VOcyTb+m1vKqzRZbDsbQ55e1OIzEwSj+ttwbjMv1w=\n===PUBLIC_KEY_END===\n"
		)
		task = SimpleNamespace(result=SimpleNamespace(output=output, is_success=True))

		with patch(
			"atlas.metal_server.core.host_installation.SSHTask.create_for_script_file",
			return_value=task,
		) as create_for_script_file:
			MetalServer._configure_wireguard(server)

		arguments = create_for_script_file.call_args.kwargs
		self.assertEqual(
			arguments["environment"],
			{
				"WIREGUARD_ADDRESS": SERVER_MESH_ADDRESS,
				"WIREGUARD_LISTEN_PORT": 51820,
				"MESH_UPLINK_INTERFACE": "eno1.1878",
				"ATLAS_WIREGUARD_ADDRESS": "fdaa:1::ffff:ffff:ffff:ffff",
				"ATLAS_WIREGUARD_PUBLIC_KEY": "atlas-key",
			},
		)
		self.assertFalse(arguments["run_in_background"])
		server.db_set.assert_any_call("wireguard_ip_address", SERVER_MESH_ADDRESS)
		server.db_set.assert_called_with(
			"wireguard_public_key", "C9VOcyTb+m1vKqzRZbDsbQ55e1OIzEwSj+ttwbjMv1w="
		)

	def test_configure_wireguard_ignores_a_progress_line_between_the_markers(self) -> None:
		server = self._server(status="Running")
		output = (
			"===PUBLIC_KEY_START===\nC9VOcyTb+m1vKqzRZbDsbQ55e1OIzEwSj+ttwbjMv1w=\n"
			"==> Atlas peer (fdaa:1::ffff:ffff:ffff:ffff)\n===PUBLIC_KEY_END===\n"
		)
		task = SimpleNamespace(result=SimpleNamespace(output=output, is_success=True))

		with (
			patch(
				"atlas.metal_server.core.host_installation.SSHTask.create_for_script_file",
				return_value=task,
			),
		):
			MetalServer._configure_wireguard(server)

		server.db_set.assert_called_with(
			"wireguard_public_key", "C9VOcyTb+m1vKqzRZbDsbQ55e1OIzEwSj+ttwbjMv1w="
		)

	def test_configure_wireguard_needs_the_atlas_identity(self) -> None:
		"""Only configure-atlas-wireguard creates the key, so two first provisions cannot make two keys."""
		server = self._server(status="Running")

		server.settings.wireguard_public_key = None

		with (
			patch(
				"atlas.metal_server.core.host_installation.SSHTask.create_for_script_file"
			) as create_for_script_file,
			self.assertRaisesRegex(frappe.ValidationError, "configure-atlas-wireguard"),
		):
			MetalServer._configure_wireguard(server)

		create_for_script_file.assert_not_called()

	def test_configure_wireguard_job_needs_the_mesh_uplink(self) -> None:
		server = self._server(status="Running")
		server.private_network_interface = None

		with (
			patch("atlas.metal_server.core.host_installation.frappe.throw", side_effect=ValueError),
			patch(
				"atlas.metal_server.core.host_installation.SSHTask.create_for_script_file"
			) as create_for_script_file,
			self.assertRaises(ValueError),
		):
			MetalServer._configure_wireguard(server)

		create_for_script_file.assert_not_called()

	def test_configure_wireguard_job_rejects_output_without_a_public_key(self) -> None:
		"""A successful run that prints no key must not store a marker as the key."""
		server = self._server(status="Running")
		task = SimpleNamespace(result=SimpleNamespace(output="==> packages\n", is_success=True))

		with (
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
			patch(
				"atlas.metal_server.core.host_installation.SSHTask.create_for_script_file",
				return_value=task,
			),
		):
			with self.assertRaises(ValueError):
				MetalServer._configure_wireguard(server)

	def test_configure_wireguard_job_rejects_a_failed_run(self) -> None:
		server = self._server(status="Running")
		task = SimpleNamespace(
			result=SimpleNamespace(output="wg: command not found", is_success=False, exit_code=127)
		)

		with (
			patch(
				"atlas.metal_server.core.host_installation.SSHTask.create_for_script_file",
				return_value=task,
			),
			self.assertRaises(frappe.ValidationError) as raised,
		):
			MetalServer._configure_wireguard(server)

		self.assertIn("wg: command not found", str(raised.exception))

	def test_configure_wireguard_rejects_a_server_that_is_not_running(self) -> None:
		server = self._server(status="Stopped")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
			patch(
				"atlas.metal_server.core.host_installation.SSHTask.create_for_script_file"
			) as create_for_script_file,
		):
			with self.assertRaises(ValueError):
				MetalServer.configure_wireguard(server)

		create_for_script_file.assert_not_called()

	def test_poweroff_server_marks_the_server_stopped(self) -> None:
		server = self._server(status="Running")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=False),
		):
			MetalServer.poweroff_server(server)

		server.settings.server_provider_controller.set_power_state.assert_called_once_with(
			"server-id", ServerPowerAction.STOP
		)
		server.db_set.assert_called_once_with("status", "Stopped")

	def test_poweron_server_marks_a_provisioned_server_running(self) -> None:
		server = self._server(status="Stopped")
		server.is_provisioning_completed = True

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=False),
		):
			MetalServer.poweron_server(server)

		server.db_set.assert_called_once_with("status", "Running")

	def test_poweron_server_keeps_the_status_while_provisioning(self) -> None:
		server = self._server(status="Failed")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=False),
		):
			MetalServer.poweron_server(server)

		server.settings.server_provider_controller.set_power_state.assert_called_once_with(
			"server-id", ServerPowerAction.START
		)
		server.db_set.assert_not_called()

	def test_reboot_server_keeps_the_status(self) -> None:
		server = self._server(status="Running")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=False),
		):
			MetalServer.reboot_server(server)

		server.settings.server_provider_controller.set_power_state.assert_called_once_with(
			"server-id", ServerPowerAction.REBOOT
		)
		server.db_set.assert_not_called()

	def test_power_action_rejects_a_deleted_server(self) -> None:
		server = self._server(status="Deleted")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
		):
			with self.assertRaises(ValueError):
				MetalServer.reboot_server(server)

	def test_power_action_rejects_a_running_setup_job(self) -> None:
		server = self._server(status="Installing")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=True),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
		):
			with self.assertRaises(ValueError):
				MetalServer.poweroff_server(server)

	def test_archive_server_deletes_the_provider_server(self) -> None:
		server = self._server(status="Failed")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=False),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.db.exists", return_value=False
			),
		):
			MetalServer.archive_server(server)

		server.settings.server_provider_controller.delete_server.assert_called_once_with("server-id", {})
		server.db_set.assert_called_once_with({"status": "Deleted", "is_provisioning_completed": 0})

	def test_archive_server_skips_a_deleted_server(self) -> None:
		server = self._server(status="Deleted")

		with patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"):
			MetalServer.archive_server(server)

		server.settings.server_provider_controller.delete_server.assert_not_called()
		server.db_set.assert_not_called()

	def test_archive_server_rejects_a_running_setup_job(self) -> None:
		server = self._server(status="Installing")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=True),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.throw", side_effect=ValueError
			),
		):
			with self.assertRaises(ValueError):
				MetalServer.archive_server(server)

	def test_archive_server_rejects_a_server_with_a_virtual_machine(self) -> None:
		server = self._server(status="Running")

		with (
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.only_for"),
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=False),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.db.exists", return_value=True
			) as exists,
			self.assertRaises(frappe.ValidationError),
		):
			MetalServer.archive_server(server)

		exists.assert_called_once_with("Virtual Machine", {"server": SERVER_NAME})
		server.settings.server_provider_controller.delete_server.assert_not_called()

	@staticmethod
	def _server(*, status: str) -> SimpleNamespace:
		server = SimpleNamespace(
			doctype="Metal Server",
			name=SERVER_NAME,
			status=status,
			provider_server_id="server-id",
			provider_metadata="{}",
			is_provisioning_completed=False,
			setup_job_id=f"atlas||server-provision||{SERVER_NAME}",
			wireguard_job_id=f"atlas||server-wireguard||{SERVER_NAME}",
			metald_job_id=f"atlas||server||metald||{SERVER_NAME}",
			wireguard_ip_address=None,
			private_ipv4_address="10.0.0.7",
			public_ipv4_address="203.0.113.7",
			private_network_interface="eno1.1878",
			ssh_host="192.0.2.7",
			port=51820,
			settings=SimpleNamespace(
				server_provider_controller=SimpleNamespace(
					delete_server=Mock(),
					set_power_state=Mock(),
					storage_pool_device=Mock(return_value="/dev/md2"),
					get_storage_pool_device=Mock(return_value="/dev/md2"),
				),
				metald_binary_x86_64_file=None,
				metald_binary_hash="metald-binary-sha256",
				wg_mesh_binary_x86_64_file="wg-mesh-file",
				wg_mesh_binary_hash="wg-mesh-binary-sha256",
				wildcard_domain="example.test",
				region_id=1,
				private_network_mtu=1500,
				private_network_cidr="10.0.0.0/20",
				wireguard_ip_address="fdaa:1::ffff:ffff:ffff:ffff",
				wireguard_public_key="atlas-key",
			),
			set=Mock(),
			save=Mock(),
			_enqueue_setup_server=Mock(),
			_provider_metadata=MetalServer._provider_metadata,
		)

		def db_set(fieldname, value=None, **_options) -> None:
			"""Write the fields on the fake document, as Document.db_set does."""
			values = fieldname if isinstance(fieldname, dict) else {fieldname: value}
			for name, field_value in values.items():
				setattr(server, name, field_value)

		server.db_set = Mock(side_effect=db_set)
		server._parse_disks = MethodType(MetalServer._parse_disks, server)
		server._get_wireguard_ip_address = MethodType(MetalServer._get_wireguard_ip_address, server)
		server._set_wireguard_ip_address_if_not_set = MethodType(
			MetalServer._set_wireguard_ip_address_if_not_set, server
		)
		server._validate_power_action = MethodType(MetalServer._validate_power_action, server)
		server._provider_server_id = MethodType(MetalServer._provider_server_id, server)
		return server


class TestExpiringCertificateRenewal(UnitTestCase):
	def test_renewal_queues_each_server_inside_the_window(self) -> None:
		server = Mock(metald_job_id=f"atlas||server||metald||{SERVER_NAME}")

		with (
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.is_certificate_authority_expiring",
				return_value=False,
			),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.now_datetime",
				return_value=datetime(2026, 9, 20),
			),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.add_days",
				return_value=datetime(2026, 10, 20),
			) as add_days,
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.get_all",
				return_value=[SERVER_NAME],
			) as get_all,
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.get_doc", return_value=server),
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=False),
		):
			renew_expiring_tls_certificates()

		server.enqueue_tls_certificate_renewal.assert_called_once_with()
		add_days.assert_called_once_with(datetime(2026, 9, 20), CERTIFICATE_RENEWAL_WINDOW_DAYS)
		self.assertEqual(
			get_all.call_args.kwargs["filters"],
			{
				"status": "Running",
				"is_provisioning_completed": 1,
				"metald_tls_expires_on": ["<=", datetime(2026, 10, 20)],
			},
		)

	def test_renewal_skips_a_server_with_a_running_metald_job(self) -> None:
		server = Mock(metald_job_id=f"atlas||server||metald||{SERVER_NAME}")

		with (
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.is_certificate_authority_expiring",
				return_value=False,
			),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.now_datetime",
				return_value=datetime(2026, 9, 20),
			),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.add_days",
				return_value=datetime(2026, 10, 20),
			),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.frappe.get_all",
				return_value=[SERVER_NAME],
			),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.get_doc", return_value=server),
			patch("atlas.metal_server.doctype.metal_server.metal_server.is_job_enqueued", return_value=True),
		):
			renew_expiring_tls_certificates()

		server.enqueue_tls_certificate_renewal.assert_not_called()

	def test_renewal_reports_an_expiring_certificate_authority(self) -> None:
		with (
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.is_certificate_authority_expiring",
				return_value=True,
			),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.now_datetime",
				return_value=datetime(2026, 9, 20),
			),
			patch(
				"atlas.metal_server.doctype.metal_server.metal_server.add_days",
				return_value=datetime(2026, 10, 20),
			),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.get_all", return_value=[]),
			patch("atlas.metal_server.doctype.metal_server.metal_server.frappe.log_error") as log_error,
		):
			renew_expiring_tls_certificates()

		log_error.assert_called_once()
