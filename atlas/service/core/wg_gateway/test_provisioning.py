from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

import atlas.service.core.wg_gateway.provisioning as provisioning
from atlas.service.core.wg_gateway.provisioning import WireGuardGatewayProvisioner

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
	machine = Mock(**({"name": "vm-00001", "server": "metal-1"} | values))
	machine.get_metal_vm_info.return_value = metal_information("151.115.112.94")
	return machine


def metal_information(public_ipv4: str) -> SimpleNamespace:
	return SimpleNamespace(desired=SimpleNamespace(network=SimpleNamespace(public_ipv4=public_ipv4)))


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
			patch.object(
				provisioning.SSHTask, "create_for_script_file", return_value=install_task
			) as create_task,
			patch.object(provisioning.SSHTask, "create_for_command", return_value=key_task),
		):
			provisioner.install_gateway()
		return create_task

	def test_the_installer_receives_the_mesh_and_port(self) -> None:
		create_task = self.install(
			SimpleNamespace(is_success=True), SimpleNamespace(is_success=True, output="pubkey\n", exit_code=0)
		)

		arguments = create_task.call_args.kwargs
		self.assertEqual(
			arguments["environment"],
			PACKAGE_ENVIRONMENT | {"GATEWAY_MESH": "fdaa:1::99", "LISTEN_PORT": 51820},
		)
