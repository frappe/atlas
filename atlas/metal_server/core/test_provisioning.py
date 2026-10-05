from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.base import ProviderOperationError
from atlas.metal_server.core.provisioning import ServerProvisioner


class TestServerProvisioner(UnitTestCase):
	def test_run_uses_the_safe_setup_order(self) -> None:
		server = self.server()
		provider = Mock(is_registration_only=False)
		provisioner = ServerProvisioner(server, provider)
		provisioner.host_installation = Mock()
		operations = Mock()
		server.ensure_provider_server.side_effect = lambda: operations("provider-create")
		provider.prepare_server.side_effect = lambda _server: operations("provider-preparation")
		provisioner.wait_for_root_ssh = Mock(side_effect=lambda: operations("secure-shell"))
		provider.configure_server_network.side_effect = lambda _server: operations("provider-network")
		provisioner.host_installation.configure_wireguard.side_effect = lambda: operations("wireguard")
		provisioner.wait_for_wireguard_ssh = Mock(side_effect=lambda: operations("wireguard-link"))
		provisioner.host_installation.install_metal.side_effect = lambda: operations("metal")

		with patch("atlas.metal_server.core.provisioning.frappe.db", SimpleNamespace(commit=Mock())):
			provisioner.run()

		self.assertEqual(
			[call.args[0] for call in operations.call_args_list],
			[
				"provider-create",
				"provider-preparation",
				"secure-shell",
				"provider-network",
				"wireguard",
				"wireguard-link",
				"metal",
			],
		)
		self.assertEqual(server.status, "Running")
		self.assertEqual(server.is_provisioning_completed, 1)
		server.enqueue_disk_sync.assert_called_once_with()
		# The provider sets the mesh MAC in memory, and progress saves it with the other setup fields.
		self.assertEqual(server.db_set.call_args.args[0]["private_network_mac_address"], "aa:bb:cc:dd:ee:01")

	def test_run_keeps_progress_and_reports_the_failed_phase(self) -> None:
		server = self.server()
		provider = Mock(is_registration_only=False)
		provider.configure_server_network.side_effect = RuntimeError("network failed")
		provisioner = ServerProvisioner(server, provider)
		provisioner.wait_for_root_ssh = Mock()
		provisioner.host_installation = Mock()

		with (
			patch("atlas.metal_server.core.provisioning.frappe.db", SimpleNamespace(commit=Mock())),
			patch("atlas.metal_server.core.provisioning.frappe.log_error") as log_error,
			self.assertRaises(RuntimeError),
		):
			provisioner.run()

		self.assertEqual(server.status, "Failed")
		server.enqueue_disk_sync.assert_not_called()
		self.assertGreaterEqual(server.db_set.call_count, 3)
		self.assertIn("provider-network", log_error.call_args.kwargs["title"])

	def test_a_retry_runs_each_idempotent_step_again(self) -> None:
		server = self.server()
		provider = Mock(is_registration_only=False)
		provisioner = ServerProvisioner(server, provider)
		provisioner.wait_for_root_ssh = Mock()
		provisioner.wait_for_wireguard_ssh = Mock()
		provisioner.host_installation = Mock()

		with patch("atlas.metal_server.core.provisioning.frappe.db", SimpleNamespace(commit=Mock())):
			provisioner.run()
			server.is_provisioning_completed = 0
			provisioner.run()

		self.assertEqual(provider.prepare_server.call_count, 2)
		self.assertEqual(server.ensure_provider_server.call_count, 2)
		self.assertEqual(provider.configure_server_network.call_count, 2)
		self.assertEqual(provisioner.host_installation.install_metal.call_count, 2)

	def test_provider_creation_failure_marks_the_pending_host_failed(self) -> None:
		server = self.server()
		server.provider_server_id = None
		server.ensure_provider_server.side_effect = RuntimeError("provider failed")
		provisioner = ServerProvisioner(server, Mock(is_registration_only=False))

		with (
			patch("atlas.metal_server.core.provisioning.frappe.db", SimpleNamespace(commit=Mock())),
			patch("atlas.metal_server.core.provisioning.frappe.log_error") as log_error,
			self.assertRaisesRegex(RuntimeError, "provider failed"),
		):
			provisioner.run()

		self.assertEqual(server.status, "Failed")
		self.assertIn("provider-create", log_error.call_args.kwargs["title"])

	def test_wireguard_link_publishes_the_host_peer_then_waits_for_root_on_wg0(self) -> None:
		"""Setup must not wait for the scheduler to add the new host to atlas0."""
		provisioner = ServerProvisioner(self.server(), Mock())

		with (
			patch("atlas.metal_server.core.provisioning.AtlasPeer") as atlas_peer,
			patch("atlas.metal_server.core.provisioning.wait_for_server") as wait_for_server,
		):
			provisioner.wait_for_wireguard_ssh()

		atlas_peer.return_value.write_config.assert_called_once_with()
		self.assertEqual(wait_for_server.call_args.kwargs["host"], "fdab:1::1")
		self.assertEqual(wait_for_server.call_args.kwargs["users"], ("root",))

	def test_a_setup_retry_after_wireguard_needs_no_public_ipv4(self) -> None:
		server = self.server()
		server.public_ipv4_address = None
		provider = Mock(ssh_users=("root",))
		provisioner = ServerProvisioner(server, provider)

		with patch("atlas.metal_server.core.provisioning.wait_for_server", return_value="root") as wait:
			provisioner.wait_for_root_ssh()

		self.assertEqual(wait.call_args.kwargs["host"], "fdab:1::1")

	def test_an_unreachable_wireguard_link_is_a_retryable_failure(self) -> None:
		provisioner = ServerProvisioner(self.server(), Mock())

		with (
			patch("atlas.metal_server.core.provisioning.AtlasPeer"),
			patch("atlas.metal_server.core.provisioning.wait_for_server", side_effect=TimeoutError),
			self.assertRaisesRegex(ProviderOperationError, "through wg0") as raised,
		):
			provisioner.wait_for_wireguard_ssh()

		self.assertTrue(raised.exception.is_retryable)

	@staticmethod
	def server() -> SimpleNamespace:
		server = SimpleNamespace(
			name="node-test-00001",
			status="Pending",
			is_provisioning_completed=0,
			provider_server_id="server-id",
			ensure_provider_server=Mock(),
			provider_metadata="{}",
			public_ipv4_address="203.0.113.1",
			private_ipv4_address="10.1.0.2",
			public_network_interface="eno1",
			private_network_interface="eno1.123",
			private_network_mac_address="aa:bb:cc:dd:ee:01",
			wireguard_ip_address="fdab:1::1",
			wireguard_public_key="public-key",
			ssh_host="fdab:1::1",
			settings=SimpleNamespace(),
			db_set=Mock(),
			enqueue_disk_sync=Mock(),
		)
		server.get = lambda field: getattr(server, field)
		return server
