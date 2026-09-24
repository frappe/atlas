from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

import atlas.service.core.wg_gateway.provisioning as provisioning
from atlas.service.core.wg_gateway.provisioning import WireGuardGatewayProvisioner
from atlas.vm.core.models import Route

PACKAGE_ENVIRONMENT = {
	"PACKAGE_NAME": "wg-gateway",
	"PACKAGE_SETUP_SCRIPT": "setup.sh",
	"PACKAGE_DOWNLOAD_URL": "https://atlas.example.com/files/wg-gateway.tar",
	"PACKAGE_SHA256": "sha-1",
}


def gateway(**values) -> SimpleNamespace:
	defaults = {
		"name": "WireGuard Gateway Server",
		"status": "Provisioning",
		"virtual_machine": "vm-00001",
		"listen_port": 51820,
		"wireguard_mesh_ipv6": "fdaa:1::99",
		"gateway_public_key": None,
		"installation_task": None,
		"save": Mock(),
	}
	return SimpleNamespace(**(defaults | values))


def virtual_machine(**values) -> Mock:
	machine = Mock(**({"name": "vm-00001", "server": "metal-1", "is_network_gateway": 0} | values))
	machine.get_metal_vm_info.return_value = metal_information("151.115.112.94")
	return machine


def metal_information(public_ipv4: str) -> SimpleNamespace:
	return SimpleNamespace(desired=SimpleNamespace(network=SimpleNamespace(public_ipv4=public_ipv4)))


class TestGatewayNetwork(UnitTestCase):
	def test_the_vm_becomes_a_gateway_with_the_internet_route(self) -> None:
		machine = virtual_machine()
		provisioner = WireGuardGatewayProvisioner(gateway())
		with (
			patch.object(provisioning.frappe, "get_doc", return_value=machine),
			patch("atlas.vm.core.vm_service.VirtualMachineService") as service,
		):
			service.return_value.get_routes.return_value = []
			provisioner.configure_network()

		machine.set_network_gateway.assert_called_once_with(True)
		network_service = service.return_value
		network_service.update_network.assert_called_once_with(
			{"routes": network_service.get_routes_with.return_value}
		)
		network_service.get_routes_with.assert_called_once_with(Route("2000::/3", "host"))

	def test_a_configured_vm_is_not_changed_again(self) -> None:
		machine = virtual_machine(is_network_gateway=1)
		provisioner = WireGuardGatewayProvisioner(gateway())
		with (
			patch.object(provisioning.frappe, "get_doc", return_value=machine),
			patch("atlas.vm.core.vm_service.VirtualMachineService") as service,
		):
			service.return_value.get_routes.return_value = [Route("2000::/3", "host")]
			provisioner.configure_network()

		machine.set_network_gateway.assert_not_called()
		service.return_value.update_network.assert_not_called()


class TestGatewayInstallation(UnitTestCase):
	def install(self, install_result, key_result) -> Mock:
		provisioner = WireGuardGatewayProvisioner(gateway())
		install_task = SimpleNamespace(name="task-1", result=install_result)
		key_task = SimpleNamespace(name="task-2", result=key_result)
		with (
			patch.object(
				provisioning.WG_GATEWAY_PACKAGE.__class__,
				"get_install_environment",
				return_value=PACKAGE_ENVIRONMENT,
			),
			patch.object(provisioning.frappe, "get_single", return_value=SimpleNamespace(region_id=3)),
			patch.object(
				provisioning.SSHTask, "create_for_script_file", return_value=install_task
			) as create_task,
			patch.object(provisioning.SSHTask, "create_for_command", return_value=key_task),
		):
			provisioner.install_gateway()
		return create_task

	def test_the_installer_receives_the_region_mesh_and_port(self) -> None:
		create_task = self.install(
			SimpleNamespace(is_success=True), SimpleNamespace(is_success=True, output="pubkey\n", exit_code=0)
		)

		arguments = create_task.call_args.kwargs
		self.assertEqual(
			arguments["environment"],
			PACKAGE_ENVIRONMENT | {"REGION_ID": 3, "GATEWAY_MESH": "fdaa:1::99", "LISTEN_PORT": 51820},
		)
