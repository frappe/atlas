from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.server.core.provisioning import ServerProvisioner


class TestServerProvisioner(UnitTestCase):
	def test_run_uses_the_safe_setup_order(self) -> None:
		server = self.server()
		provider = Mock()
		provisioner = ServerProvisioner(server, provider)
		provisioner.host_installation = Mock()
		operations = Mock()
		provider.prepare_server.side_effect = lambda _server: operations("provider-preparation")
		provisioner.wait_for_root_ssh = Mock(side_effect=lambda: operations("secure-shell"))
		provider.configure_server_network.side_effect = lambda _server: operations("provider-network")
		provisioner.host_installation.configure_wireguard.side_effect = lambda: operations("wireguard")
		provisioner.host_installation.install_metal.side_effect = lambda: operations("metal")

		with patch("atlas.server.core.provisioning.frappe.db", SimpleNamespace(commit=Mock())):
			provisioner.run()

		self.assertEqual(
			[call.args[0] for call in operations.call_args_list],
			["provider-preparation", "secure-shell", "provider-network", "wireguard", "metal"],
		)
		self.assertEqual(server.status, "Running")
		self.assertEqual(server.is_provisioning_completed, 1)

	def test_run_keeps_progress_and_reports_the_failed_phase(self) -> None:
		server = self.server()
		provider = Mock()
		provider.configure_server_network.side_effect = RuntimeError("network failed")
		provisioner = ServerProvisioner(server, provider)
		provisioner.wait_for_root_ssh = Mock()
		provisioner.host_installation = Mock()

		with (
			patch("atlas.server.core.provisioning.frappe.db", SimpleNamespace(commit=Mock())),
			patch("atlas.server.core.provisioning.frappe.log_error") as log_error,
			self.assertRaises(RuntimeError),
		):
			provisioner.run()

		self.assertEqual(server.status, "Failed")
		self.assertGreaterEqual(server.db_set.call_count, 3)
		self.assertIn("provider-network", log_error.call_args.kwargs["title"])

	def test_a_retry_runs_each_idempotent_step_again(self) -> None:
		server = self.server()
		provider = Mock()
		provisioner = ServerProvisioner(server, provider)
		provisioner.wait_for_root_ssh = Mock()
		provisioner.host_installation = Mock()

		with patch("atlas.server.core.provisioning.frappe.db", SimpleNamespace(commit=Mock())):
			provisioner.run()
			server.is_provisioning_completed = 0
			provisioner.run()

		self.assertEqual(provider.prepare_server.call_count, 2)
		self.assertEqual(provider.configure_server_network.call_count, 2)
		self.assertEqual(provisioner.host_installation.install_metal.call_count, 2)

	@staticmethod
	def server() -> SimpleNamespace:
		server = SimpleNamespace(
			name="node-test-00001",
			status="Pending",
			is_provisioning_completed=0,
			provider_server_id="server-id",
			provider_metadata="{}",
			public_ipv4_address="203.0.113.1",
			private_ipv4_address="10.1.0.2",
			public_network_interface="eno1",
			private_network_interface="eno1.123",
			wireguard_ip_address="fdab:1::1",
			wireguard_public_key="public-key",
			db_set=Mock(),
		)
		server.get = lambda field: getattr(server, field)
		return server
