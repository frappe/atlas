from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.base import (
	ProviderOperationError,
	ProviderServer,
	ServerCreateRequest,
	ServerPowerAction,
)
from atlas.atlas.core.server_providers.scaleway.client import ScalewayError
from atlas.atlas.core.server_providers.scaleway.provider import ScalewayProvider
from atlas.atlas.core.server_providers.scaleway.servers import ScalewayServers


class TestScalewayProvider(UnitTestCase):
	def test_setup_infrastructure_saves_each_named_resource(self) -> None:
		provider = self.provider()
		provider.settings.private_network_cidr = "10.1.0.0/20"
		provider.settings.public_ssh_key = "ssh-ed25519 key"
		provider.settings.db_set = Mock()
		provider.settings.save = Mock()
		provider.infrastructure = Mock()
		provider.infrastructure.create_vpc.return_value = "vpc-id"
		provider.infrastructure.create_private_network.return_value = "network-id"
		provider.infrastructure.create_ssh_key.return_value = "ssh-key-id"

		provider.setup_infrastructure()

		self.assertEqual(
			[field_call.args[:2] for field_call in provider.settings.db_set.call_args_list],
			[
				("scaleway_vpc_id", "vpc-id"),
				("scaleway_private_network_id", "network-id"),
				("scaleway_ssh_key_id", "ssh-key-id"),
			],
		)
		self.assertEqual(provider.settings.is_server_provider_setup_completed, 1)
		provider.settings.save.assert_called_once_with()

	def test_ensure_server_delegates_to_server_operations(self) -> None:
		provider = self.provider()
		request = Mock()
		provider.servers.ensure.return_value = ProviderServer(
			provider_server_id="server-id",
			status="Installing",
			public_ipv4_address="203.0.113.1",
			provider_metadata={},
		)

		result = provider.ensure_server(request)

		self.assertEqual(result.provider_server_id, "server-id")
		provider.servers.ensure.assert_called_once_with(request)

	def test_prepare_server_runs_provider_steps_in_order(self) -> None:
		provider = self.provider()
		provider.attach_private_network = Mock()
		provider.wait_for_private_network = Mock()
		provider.wait_for_server_ready = Mock()
		server = self.server()
		operations = Mock()
		provider.attach_private_network.side_effect = lambda _server: operations("attach")
		provider.wait_for_private_network.side_effect = lambda _server: operations("network")
		provider.wait_for_server_ready.side_effect = lambda _server: operations("server")

		provider.prepare_server(server)

		self.assertEqual(
			[call.args[0] for call in operations.call_args_list], ["attach", "network", "server"]
		)

	def test_power_action_delegates_to_server_operations(self) -> None:
		provider = self.provider()

		provider.set_power_state("server-id", ServerPowerAction.REBOOT)

		provider.servers.set_power_state.assert_called_once_with("server-id", ServerPowerAction.REBOOT)

	def test_ip_address_operations_use_the_ip_address_owner(self) -> None:
		provider = self.provider()
		server = self.server(provider_server_id="server-id")

		provider.attach_public_ipv4_address("address-id", server)
		provider.detach_public_ipv4_address("address-id")
		provider.delete_public_ipv4_address("address-id")

		provider.ip_addresses.attach.assert_called_once_with("address-id", "server-id")
		provider.ip_addresses.detach.assert_called_once_with("address-id")
		provider.ip_addresses.delete.assert_called_once_with("address-id")

	def test_configure_server_network_passes_the_static_address(self) -> None:
		provider = self.provider()
		server = self.server(provider_server_id="server-id")
		server.public_network_interface = "eno1"
		server.private_network_interface = "eno1.123"
		server.private_ipv4_address = "10.1.0.10"
		server.provider_metadata = json.dumps({"private_network": {"vlan": 123}})
		provider.run_setup_script = Mock()
		provider.wait_for_private_address = Mock()

		provider.configure_server_network(server)

		environment = provider.run_setup_script.call_args.kwargs["environment"]
		self.assertEqual(environment["DEVICE"], "eno1.123")
		self.assertEqual(environment["VLAN"], 123)
		self.assertEqual(environment["ADDRESS"], "10.1.0.10/20")
		provider.wait_for_private_address.assert_called_once_with(server)

	def test_wait_for_private_address_accepts_the_expected_address(self) -> None:
		provider = self.provider()
		server = self.server()
		server.public_ipv4_address = "203.0.113.1"
		server.private_network_interface = "eno1.123"
		server.private_ipv4_address = "10.1.0.2"
		runner = Mock()
		runner.run_command.return_value = SimpleNamespace(
			exit_code=0, output="4: eno1.123 inet 10.1.0.2/20 scope global"
		)

		with patch("atlas.atlas.core.ssh.SSHRunner", return_value=runner):
			provider.wait_for_private_address(server)

		self.assertIn("eno1.123", runner.run_command.call_args.args[0])

	def test_wait_for_private_address_marks_a_timeout_as_retryable(self) -> None:
		provider = self.provider()
		server = self.server()
		server.public_ipv4_address = "203.0.113.1"
		server.private_network_interface = "eno1.123"
		server.private_ipv4_address = "10.1.0.2"
		provider.poll = Mock(side_effect=ProviderOperationError("time limit", is_retryable=True))

		with self.assertRaises(ScalewayError) as raised:
			provider.wait_for_private_address(server)

		self.assertTrue(raised.exception.is_retryable)

	def provider(self) -> ScalewayProvider:
		provider = object.__new__(ScalewayProvider)
		provider.settings = SimpleNamespace(
			private_network_cidr="10.1.0.0/20",
			private_network_mtu=1500,
		)
		provider.partitioning = SimpleNamespace(storage_array="/dev/md2")
		provider.servers = Mock()
		provider.ip_addresses = Mock()
		return provider

	@staticmethod
	def server(provider_server_id: str | None = None) -> SimpleNamespace:
		return SimpleNamespace(
			name="node-test-00001",
			provider_server_id=provider_server_id,
			public_network_interface=None,
			private_network_interface=None,
			private_ipv4_address=None,
			public_ipv4_address=None,
			provider_metadata=None,
		)


class TestScalewayServers(UnitTestCase):
	def test_create_uses_catalog_values_and_stable_identity(self) -> None:
		servers = self.servers()
		servers.catalog.offer.side_effect = [
			{"id": "hourly-offer", "monthly_offer_id": "monthly-offer"},
			{"options": [{"id": "network-option", "private_network": {}}]},
		]
		servers.catalog.offer_id.return_value = "hourly-offer"
		servers.catalog.private_network_option_id.return_value = "network-option"
		servers.partitioning.get_schema.return_value = None
		servers.client.request.side_effect = [{}, {"id": "server-id"}]
		request = ServerCreateRequest(
			name="node-test-00001",
			server_size="Scaleway/large",
			server_image="Scaleway/Ubuntu_26.04",
			size_provider_metadata={"hourly": {}, "monthly": {}},
			image_provider_metadata={"id": "image-id"},
		)

		servers.create(request)

		create_request = servers.client.request.call_args_list[-1]
		self.assertEqual(create_request.args[1], "/baremetal/v1/zones/fr-par-1/servers")
		self.assertEqual(create_request.kwargs["json"]["name"], "node-test-00001")
		self.assertEqual(create_request.kwargs["json"]["option_ids"], ["network-option"])
		self.assertEqual(create_request.kwargs["json"]["install"]["os_id"], "image-id")

	def test_ensure_reuses_the_server_with_the_stable_tag(self) -> None:
		servers = self.servers()
		servers.find = Mock(return_value={"id": "server-id", "status": "ready", "ips": []})
		servers.create = Mock()

		result = servers.ensure(SimpleNamespace(name="node-test-00001"))

		self.assertEqual(result.provider_server_id, "server-id")
		self.assertFalse(result.was_created)
		servers.create.assert_not_called()

	def test_set_power_state_uses_the_explicit_action(self) -> None:
		servers = self.servers()

		servers.set_power_state("server-id", ServerPowerAction.STOP)

		self.assertEqual(
			servers.client.request.call_args.args,
			("POST", "/baremetal/v1/zones/fr-par-1/servers/server-id/stop"),
		)

	def test_delete_is_idempotent(self) -> None:
		servers = self.servers()

		servers.delete("server-id")

		self.assertTrue(servers.client.request.call_args.kwargs["allow_missing"])

	@staticmethod
	def servers() -> ScalewayServers:
		return ScalewayServers(
			client=Mock(),
			configuration=SimpleNamespace(
				zone="fr-par-1",
				region="fr-par",
				project_id="project-id",
				private_network_id="private-network-id",
				ssh_key_id="ssh-key-id",
				billing_cycle="Hourly",
			),
			catalog=Mock(),
			partitioning=Mock(),
		)
