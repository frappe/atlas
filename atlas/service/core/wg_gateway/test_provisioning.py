from __future__ import annotations

import tomllib
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

import atlas.service.core.wg_gateway.configuration as configuration
import atlas.service.core.wg_gateway.provisioning as provisioning
from atlas.service.core.wg_gateway.configuration import GatewayConfiguration
from atlas.service.core.wg_gateway.provisioning import WireGuardGatewayProvisioner

PACKAGE_ENVIRONMENT = {
	"PACKAGE_NAME": "wg-gateway",
	"PACKAGE_SETUP_SCRIPT": "setup.sh",
	"PACKAGE_DOWNLOAD_URL": "https://atlas.example.com/files/wg-gateway.tar",
	"PACKAGE_SHA256": "sha-1",
}
PASSWORDS = {
	"wildcard_tls_certificate": "-----BEGIN CERTIFICATE-----\nleaf\n-----END CERTIFICATE-----",
	"wildcard_tls_private_key": "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----",
	"wireguard_gateway_cluster_password": "cluster-password",
}


def gateway(name: str = "wireguard-001", **values) -> SimpleNamespace:
	node = SimpleNamespace(
		**(
			{
				"name": name,
				"status": "Provisioning",
				"virtual_machine": "vm-00001",
				"listen_port": 51820,
				"wireguard_mesh_ipv6": "fdaa:1::99",
				"gateway_public_key": f"{name}-public",
				"installed_package_hash": None,
				"pushed_config_hash": None,
				"dns_health_check_id": None,
				"public_ipv4": "151.115.112.94",
				"save": Mock(),
			}
			| values
		)
	)
	node.gateway_id = int(name.rsplit("-", 1)[-1])
	node.get_domain = lambda: f"{name}.par-1.example.com"
	node.get_regional_domain = lambda: "wireguard.par-1.example.com"
	node.get_password = lambda fieldname: f"{name}-private"
	return node


def settings() -> SimpleNamespace:
	return SimpleNamespace(
		wildcard_domain="par-1.example.com",
		region_id=1,
		issuer="atlas:1",
		jwks_url="https://atlas.example.com/api/atlas/jwks.json",
		wg_gateway_audience_id="atlas-wg-gateway:1",
		dns_provider_controller=Mock(),
		get_password=lambda fieldname, raise_exception=True: PASSWORDS.get(fieldname),
	)


class TestGatewayConfiguration(UnitTestCase):
	def build(self, node: SimpleNamespace, members: list[SimpleNamespace]) -> GatewayConfiguration:
		with patch.object(configuration.frappe, "get_single", return_value=settings()):
			return GatewayConfiguration(node, members)

	def test_the_file_lists_every_node_and_the_cluster(self) -> None:
		node = gateway("wireguard-001")
		document = tomllib.loads(self.build(node, [gateway("wireguard-002"), node]).content)

		self.assertEqual(
			document["gateway"],
			{"region_id": 1, "node_id": "wireguard-001", "private_key": "wireguard-001-private"},
		)
		self.assertEqual([item["gateway_id"] for item in document["nodes"]], [1, 2])
		self.assertEqual(document["nodes"][1]["endpoint"], "wireguard-002.par-1.example.com")
		self.assertEqual(document["auth"]["jwks_audience_id"], "atlas-wg-gateway:1")
		self.assertEqual(
			[peer["address"] for peer in document["cluster"]["peers"]],
			["https://wireguard-001.par-1.example.com", "https://wireguard-002.par-1.example.com"],
		)

	def test_a_new_member_changes_the_digest_of_every_node(self) -> None:
		node = gateway("wireguard-001")

		self.assertNotEqual(
			self.build(node, [node]).digest, self.build(node, [node, gateway("wireguard-002")]).digest
		)

	def test_the_apply_command_holds_no_secret_and_points_peers_at_the_mesh(self) -> None:
		node = gateway("wireguard-001")
		command = self.build(node, [node]).get_apply_command()

		self.assertNotIn("private", command)
		self.assertNotIn("cluster-password", command)
		self.assertIn("fdaa:1::99 wireguard-001.par-1.example.com", command)


class TestGatewayProvisioning(UnitTestCase):
	def provisioner(self, node: SimpleNamespace) -> WireGuardGatewayProvisioner:
		with patch.object(provisioning.frappe, "get_single", return_value=settings()):
			return WireGuardGatewayProvisioner(node)

	def test_membership_reaches_the_other_nodes_before_regional_dns(self) -> None:
		phases = [phase for phase, _ in self.provisioner(gateway()).steps]

		self.assertLess(phases.index("cluster-membership"), phases.index("regional-dns"))
		self.assertLess(phases.index("readiness"), phases.index("regional-dns"))
		self.assertLess(phases.index("package"), phases.index("configuration"))

	def test_the_installer_receives_only_the_network_values(self) -> None:
		provisioner = self.provisioner(gateway())
		task = SimpleNamespace(result=SimpleNamespace(is_success=True))
		with (
			patch.object(
				type(provisioning.WG_GATEWAY_PACKAGE),
				"get_install_environment",
				return_value=PACKAGE_ENVIRONMENT,
			),
			patch.object(provisioning.SSHTask, "create_for_script_file", return_value=task) as create,
		):
			provisioner.install_package()

		environment = create.call_args.kwargs["environment"]
		self.assertEqual(
			{key: environment[key] for key in ("REGION_ID", "GATEWAY_ID", "GATEWAY_MESH", "LISTEN_PORT")},
			{"REGION_ID": 1, "GATEWAY_ID": 1, "GATEWAY_MESH": "fdaa:1::99", "LISTEN_PORT": 51820},
		)
		self.assertFalse([key for key in environment if "JWKS" in key])

	def test_an_installed_package_is_not_installed_again(self) -> None:
		provisioner = self.provisioner(gateway(installed_package_hash="sha-1"))
		with (
			patch.object(
				type(provisioning.WG_GATEWAY_PACKAGE),
				"get_install_environment",
				return_value=PACKAGE_ENVIRONMENT,
			),
			patch.object(provisioning.SSHTask, "create_for_script_file") as create,
		):
			provisioner.install_package()

		create.assert_not_called()

	def test_the_node_joins_the_health_checked_regional_name(self) -> None:
		provisioner = self.provisioner(gateway())
		provider = provisioner.settings.dns_provider_controller
		provider.create_https_health_check.return_value = "check-1"

		provisioner.update_regional_dns_record()

		provider.create_https_health_check.assert_called_once_with(
			"151.115.112.94", "wireguard-001.par-1.example.com", "/readyz"
		)
		provider.upsert_multivalue_a_record.assert_called_once_with(
			"wireguard.par-1.example.com", "wireguard-001", "151.115.112.94", "check-1", ttl=120
		)

	def test_a_node_becomes_a_member_before_the_other_nodes_receive_the_list(self) -> None:
		node = gateway()
		provisioner = self.provisioner(node)
		pushed_members = []

		def push(self_provisioner):
			pushed_members.append(node.is_cluster_member)

		with (
			patch.object(provisioning.frappe, "get_all", return_value=["wireguard-001", "wireguard-002"]),
			patch.object(provisioning.frappe, "get_doc", return_value=gateway("wireguard-002")),
			patch.object(provisioning.frappe.db, "commit"),
			patch.object(provisioning.frappe, "get_single", return_value=settings()),
			patch.object(WireGuardGatewayProvisioner, "push_configuration", push),
		):
			provisioner.push_cluster_membership()

		self.assertEqual(pushed_members, [1])

	def test_only_members_appear_in_the_configuration(self) -> None:
		node = gateway("wireguard-003")
		with (
			patch.object(configuration.frappe, "get_single", return_value=settings()),
			patch.object(configuration.frappe, "get_all", return_value=["wireguard-001"]) as get_all,
			patch.object(configuration.frappe, "get_doc", return_value=gateway("wireguard-001")),
		):
			names = [member.name for member in GatewayConfiguration(node).nodes]

		self.assertEqual(get_all.call_args.kwargs["filters"], {"is_cluster_member": 1})
		self.assertEqual(names, ["wireguard-001", "wireguard-003"])

	def test_removing_one_of_two_nodes_updates_the_survivor_before_it_returns(self) -> None:
		"""The survivor must stop waiting for the removed node before its machine stops."""
		survivor = gateway("wireguard-001", status="Active")
		pushed = []

		def push(self_provisioner):
			pushed.append(self_provisioner.gateway.name)

		with (
			patch.object(provisioning.frappe, "get_all", return_value=["wireguard-001"]) as get_all,
			patch.object(provisioning.frappe, "get_doc", return_value=survivor),
			patch.object(provisioning.frappe, "get_single", return_value=settings()),
			patch.object(WireGuardGatewayProvisioner, "push_configuration", push),
		):
			provisioning.apply_membership_to_active_gateways(excluding="wireguard-002")

		self.assertEqual(get_all.call_args.kwargs["filters"], {"status": "Active"})
		self.assertEqual(pushed, ["wireguard-001"])

	def stop(self, result=None, error=None, observed_states=(), keeps_running=False) -> tuple[Mock, Mock]:
		runner = Mock()
		runner.return_value.run_command.side_effect = error
		runner.return_value.run_command.return_value = result
		service = Mock()
		if keeps_running:
			service.return_value.get_information.return_value = SimpleNamespace(
				observed=SimpleNamespace(state="running")
			)
		else:
			service.return_value.get_information.side_effect = [
				SimpleNamespace(observed=SimpleNamespace(state=state)) for state in observed_states
			]
		provisioner = self.provisioner(gateway())
		with (
			patch.object(provisioning.frappe.db, "exists", return_value=True),
			patch.object(provisioning.frappe, "get_doc", return_value=Mock()),
			patch.object(provisioning, "SSHRunner", runner),
			patch("atlas.vm.core.vm_service.VirtualMachineService", service),
			patch.object(provisioning.time, "sleep"),
		):
			provisioner.stop_api()
		return runner, service

	def test_the_api_stops_over_ssh_before_the_member_list_changes(self) -> None:
		runner, service = self.stop(SimpleNamespace(is_success=True, exit_code=0, output=""))

		self.assertEqual(
			runner.return_value.run_command.call_args.args[0], "systemctl stop atlas-wg-gateway-api.service"
		)
		service.return_value.set_power_state.assert_not_called()

	def test_without_ssh_metal_must_confirm_the_machine_stopped(self) -> None:
		for result, error in (
			(SimpleNamespace(is_success=False, exit_code=255, output="Connection refused"), None),
			(None, provisioning.subprocess.TimeoutExpired("ssh", 30)),
		):
			_, service = self.stop(result, error, observed_states=("running", "stopped"))
			service.return_value.set_power_state.assert_called_once_with("stopped")
			self.assertEqual(service.return_value.get_information.call_count, 2)

	def test_a_machine_that_keeps_running_fails_the_action(self) -> None:
		with (
			patch.object(provisioning, "STOP_TIMEOUT_SECONDS", 0.05),
			self.assertRaisesRegex(frappe.ValidationError, "did not confirm"),
		):
			self.stop(
				SimpleNamespace(is_success=False, exit_code=1, output="unit busy"),
				keeps_running=True,
			)
