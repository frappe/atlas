from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.base import ServerProviderError
from atlas.atlas.core.server_providers.scaleway import ScalewayError, ScalewayProvider
from atlas.atlas.core.server_providers.scaleway.partitioning import ScalewayPartitioning


class TestScalewayProvider(UnitTestCase):
	def test_provisioning_steps_return_scaleway_functions(self) -> None:
		provider = self._provider()

		step_names = tuple(step.__name__ for step in provider.provisioning_steps)

		self.assertEqual(
			step_names,
			(
				"_attach_private_network",
				"_wait_for_private_network",
				"_wait_for_server_ready",
				"wait_for_ssh",
				"_configure_private_network",
				"_wait_for_private_address",
			),
		)

	def test_create_server_reuses_tagged_server(self) -> None:
		provider = self._provider()
		remote_server = {"id": "server-id", "status": "delivering", "ips": []}
		provider._find_server.return_value = remote_server
		server = self._server()

		provider._create_server(server)

		self.assertEqual(server.provider_server_id, "server-id")
		provider._request.assert_not_called()

	def test_get_storage_pool_device_returns_the_raw_raid_array(self) -> None:
		provider = self._provider()

		self.assertEqual(provider.get_storage_pool_device(self._server()), "/dev/md2")

	def test_get_install_configuration_reserves_the_storage_array(self) -> None:
		provider = self._provider()
		provider._request.return_value = {"disks": [{"device": "/dev/nvme0n1"}, {"device": "/dev/nvme1n1"}]}
		image_document = SimpleNamespace(get_provider_metadata=Mock(return_value="os-id"))

		install = provider._get_install_configuration(self._server(), "offer-id", image_document)

		self.assertEqual(install["os_id"], "os-id")
		self.assertEqual(install["hostname"], "node-test-00001")
		self.assertEqual(
			provider._request.call_args.args,
			("GET", "/baremetal/v1/zones/fr-par-1/partitioning-schemas/default"),
		)
		self.assertEqual(
			provider._request.call_args.kwargs["params"], {"offer_id": "offer-id", "os_id": "os-id"}
		)
		arrays = [raid["name"] for raid in install["partitioning_schema"]["raids"]]
		self.assertIn(provider.get_storage_pool_device(self._server()), arrays)

	def test_get_install_configuration_keeps_the_vendor_layout_without_a_vendor_schema(self) -> None:
		"""An offer without custom partitioning answers 404, which _request maps to {}."""
		provider = self._provider()
		provider._request.return_value = {}
		image_document = SimpleNamespace(get_provider_metadata=Mock(return_value="os-id"))

		install = provider._get_install_configuration(self._server(), "offer-id", image_document)

		self.assertNotIn("partitioning_schema", install)
		self.assertTrue(provider._request.call_args.kwargs["allow_missing"])

	def test_power_actions_post_the_matching_endpoint(self) -> None:
		for action, path in (
			(ScalewayProvider.reboot_server, "reboot"),
			(ScalewayProvider.poweroff_server, "stop"),
			(ScalewayProvider.poweron_server, "start"),
		):
			with self.subTest(action=action.__name__):
				provider = self._provider()
				server = self._server(provider_server_id="server-id")

				action(provider, server)

				self.assertEqual(
					provider._request.call_args.args,
					("POST", f"/baremetal/v1/zones/fr-par-1/servers/server-id/{path}"),
				)

	def test_power_action_raises_without_a_provider_server_id(self) -> None:
		provider = self._provider()

		with self.assertRaises(ScalewayError):
			provider.reboot_server(self._server())

		provider._request.assert_not_called()

	def test_archive_server_deletes_the_scaleway_server(self) -> None:
		provider = self._provider()
		server = self._server(provider_server_id="server-id")

		provider.archive_server(server)

		method, path = provider._request.call_args.args
		self.assertEqual(method, "DELETE")
		self.assertEqual(path, "/baremetal/v1/zones/fr-par-1/servers/server-id")
		self.assertTrue(provider._request.call_args.kwargs["allow_missing"])

	def test_archive_server_skips_a_server_without_a_provider_id(self) -> None:
		provider = self._provider()

		provider.archive_server(self._server())

		provider._request.assert_not_called()

	def test_server_details_marks_delivering_server_as_installing(self) -> None:
		provider = self._provider()
		provider._update_server_details = ScalewayProvider._update_server_details.__get__(
			provider, ScalewayProvider
		)
		provider._update_provider_metadata = ScalewayProvider._update_provider_metadata
		server = self._server()
		server.status = "Pending"
		server.public_ipv4_address = None

		provider._update_server_details(server, {"status": "delivering", "ips": []})

		self.assertEqual(server.status, "Installing")
		self.assertEqual(frappe.parse_json(server.provider_metadata)["server"]["status"], "delivering")

	def test_attach_private_network_reuses_existing_attachment(self) -> None:
		provider = self._provider()
		provider._server_private_network.return_value = {
			"id": "nic-id",
			"vlan": 123,
			"status": "attached",
		}
		provider._request.return_value = {"ips": [{"address": "10.1.0.2/20", "is_ipv6": False}]}
		server = self._server(provider_server_id="server-id")

		provider._attach_private_network(server)

		self.assertEqual(server.public_network_interface, "eno1")
		self.assertEqual(server.private_network_interface, "eno1.123")
		self.assertEqual(server.private_ipv4_address, "10.1.0.2")

	def test_private_ipv4_address_skips_the_ipv6_address(self) -> None:
		provider = self._provider()
		provider._request.return_value = {
			"ips": [
				{"address": "fd46:865f:c559:e84::1/64", "is_ipv6": True},
				{"address": "10.1.0.2/20", "is_ipv6": False},
			]
		}

		self.assertEqual(provider.get_private_ipv4_address("nic-id"), "10.1.0.2")

	def test_private_ipv4_address_raises_without_an_ipv4_address(self) -> None:
		provider = self._provider()
		provider._request.return_value = {"ips": [{"address": "fd46::1/64", "is_ipv6": True}]}

		with self.assertRaises(ScalewayError):
			provider.get_private_ipv4_address("nic-id")

	def test_wait_for_private_address_accepts_the_expected_address(self) -> None:
		provider = self._provider()
		server = self._server()
		server.public_ipv4_address = "203.0.113.1"
		server.private_network_interface = "eno1.123"
		server.private_ipv4_address = "10.1.0.2"
		runner = Mock()
		runner.run_command.return_value = SimpleNamespace(
			exit_code=0, output="4: eno1.123    inet 10.1.0.2/20 scope global"
		)

		with patch("atlas.atlas.core.ssh.SSHRunner", return_value=runner):
			provider._wait_for_private_address(server)

		self.assertIn("eno1.123", runner.run_command.call_args.args[0])

	def test_wait_for_private_address_raises_when_the_address_never_appears(self) -> None:
		provider = self._provider()
		provider.private_address_attempts = 2
		server = self._server()
		server.public_ipv4_address = "203.0.113.1"
		server.private_network_interface = "eno1.123"
		server.private_ipv4_address = "10.1.0.2"
		runner = Mock()
		runner.run_command.return_value = SimpleNamespace(exit_code=1, output="")
		provider.poll = Mock(side_effect=ServerProviderError("timed out"))

		with (
			patch("atlas.atlas.core.ssh.SSHRunner", return_value=runner),
			self.assertRaises(ScalewayError),
		):
			provider._wait_for_private_address(server)

	def test_server_private_network_uses_the_list_endpoint(self) -> None:
		provider = self._provider()
		provider._request.return_value = {
			"server_private_networks": [
				{"private_network_id": "private-network-id", "vlan": 123, "status": "attached"}
			]
		}

		private_network = provider._server_private_network("server-id")

		self.assertEqual(private_network["vlan"], 123)
		self.assertEqual(
			provider._request.call_args.args,
			("GET", "/baremetal/v1/zones/fr-par-1/server-private-networks"),
		)
		self.assertEqual(provider._request.call_args.kwargs["params"]["server_id"], "server-id")

	def test_wait_for_server_ready_polls_until_the_os_install_completes(self) -> None:
		provider = self._provider()
		provider._request.side_effect = [
			{"status": "ready", "install": {"status": "installing"}, "ips": []},
			{"status": "ready", "install": {"status": "completed"}, "ips": []},
		]
		server = self._server(provider_server_id="server-id")

		with patch("atlas.atlas.core.server_providers.base.sleep") as sleep_mock:
			provider._wait_for_server_ready(server)

		self.assertEqual(provider._request.call_count, 2)
		sleep_mock.assert_called_once_with(provider.setup_poll_interval_seconds)

	def test_wait_for_server_ready_raises_on_a_failed_os_install(self) -> None:
		provider = self._provider()
		provider._request.return_value = {"status": "ready", "install": {"status": "error"}, "ips": []}
		server = self._server(provider_server_id="server-id")

		with self.assertRaises(ScalewayError):
			provider._wait_for_server_ready(server)

	def test_wait_for_private_network_polls_until_attached(self) -> None:
		provider = self._provider()
		provider._server_private_network.side_effect = [
			{"vlan": 123, "status": "attaching"},
			{"vlan": 123, "status": "attached"},
		]
		server = self._server(provider_server_id="server-id")

		with patch("atlas.atlas.core.server_providers.base.sleep") as sleep_mock:
			provider._wait_for_private_network(server)

		provider.save_server_setup_progress.assert_called_once_with(server)
		sleep_mock.assert_called_once_with(provider.setup_poll_interval_seconds)

	def test_promote_ssh_user_runs_the_ubuntu_script(self) -> None:
		provider = self._provider()
		server = self._server()

		provider.promote_ssh_user(server, "ubuntu")

		provider.run_setup_script.assert_called_once_with(
			server, "scaleway/promote-ubuntu-user.sh", ssh_user="ubuntu"
		)

	def test_configure_private_network_passes_the_static_address(self) -> None:
		provider = self._provider()
		server = self._server(provider_server_id="server-id")
		server.public_network_interface = "eno1"
		server.private_network_interface = "eno1.123"
		server.provider_metadata = json.dumps({"private_network": {"vlan": 123}})
		server.private_ipv4_address = "10.1.0.10"
		server.public_ipv4_address = "203.0.113.1"

		provider._configure_private_network(server)

		self.assertEqual(
			provider.run_setup_script.call_args.args, (server, "scaleway/configure-private-network.sh")
		)
		environment = provider.run_setup_script.call_args.kwargs["environment"]
		self.assertEqual(environment["PARENT_INTERFACE"], "eno1")
		self.assertEqual(environment["DEVICE"], "eno1.123")
		self.assertEqual(environment["VLAN"], 123)
		self.assertEqual(environment["ADDRESS"], "10.1.0.10/20")

	def _provider(self) -> ScalewayProvider:
		provider = object.__new__(ScalewayProvider)
		provider.project_id = "project-id"
		provider.zone = "fr-par-1"
		provider.settings = SimpleNamespace(
			private_network_cidr="10.1.0.0/20",
			private_network_mtu=1500,
			scaleway_machine_billing_cycle="Hourly",
			scaleway_private_network_id="private-network-id",
			scaleway_ssh_key_id="ssh-key-id",
		)
		provider.partitioning = ScalewayPartitioning()
		provider._find_server = Mock()
		provider._request = Mock()
		provider._server_private_network = Mock()
		provider._update_server_details = Mock()
		provider._update_provider_metadata = Mock()
		provider.save_server_setup_progress = Mock()
		provider.run_setup_script = Mock()
		return provider

	@staticmethod
	def _server(provider_server_id: str | None = None) -> SimpleNamespace:
		return SimpleNamespace(
			name="node-test-00001",
			provider_server_id=provider_server_id,
			server_image="Scaleway/Ubuntu_26.04",
			server_size="Scaleway/EM-A410X",
			public_network_interface=None,
			private_network_interface=None,
			private_ipv4_address=None,
			provider_metadata=None,
		)
