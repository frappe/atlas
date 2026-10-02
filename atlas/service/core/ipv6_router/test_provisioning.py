from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

import atlas.service.core.ipv6_router.provisioning as provisioning
from atlas.service.core.ipv6_router.provisioning import IPv6RouterServerProvisioner
from atlas.vm.core.models import Route

PACKAGE_ENVIRONMENT = {
	"PACKAGE_NAME": "ipv6-router",
	"PACKAGE_SETUP_SCRIPT": "setup.sh",
	"PACKAGE_DOWNLOAD_URL": "https://atlas.example.com/files/ipv6-router.tar",
	"PACKAGE_SHA256": "sha-1",
}


def router(**values) -> SimpleNamespace:
	defaults = {
		"name": "IPv6 Router Server",
		"status": "Provisioning",
		"virtual_machine": "vm-00001",
		"prefix": "2001:db8::/64",
		"pool": SimpleNamespace(prefix="2001:db8::/64", begin_provider_attach=Mock(), reconcile=Mock()),
		"installation_task": None,
		"save": Mock(),
	}
	return SimpleNamespace(**(defaults | values))


def virtual_machine(**values) -> Mock:
	machine = Mock(**({"name": "vm-00001", "server": "metal-1", "is_network_gateway": 0} | values))
	machine.get_metal_vm_info.return_value = metal_information("151.115.112.94", "")
	return machine


def metal_information(public_ipv4: str, public_ipv6: str = "") -> SimpleNamespace:
	return SimpleNamespace(
		desired=SimpleNamespace(network=SimpleNamespace(public_ipv4=public_ipv4, public_ipv6=public_ipv6))
	)


class TestRouterReadiness(UnitTestCase):
	def is_ready(self, public_ipv4: str) -> bool:
		machine = virtual_machine(is_draft=0)
		machine.get_metal_vm_info.return_value = metal_information(public_ipv4)
		with patch.object(provisioning.frappe, "get_doc", return_value=machine):
			return IPv6RouterServerProvisioner(router()).is_virtual_machine_ready

	def test_the_router_needs_no_public_ipv4(self) -> None:
		self.assertTrue(self.is_ready(""))


class TestRouterNetwork(UnitTestCase):
	def configure(self, machine: Mock) -> None:
		provisioner = IPv6RouterServerProvisioner(router())
		with (
			patch.object(provisioning.frappe, "get_doc", return_value=machine),
			patch("atlas.vm.core.vm_service.VirtualMachineService") as service,
		):
			self.network_service = service
			provisioner.configure_network()

	def test_the_vm_becomes_a_gateway_and_holds_the_block(self) -> None:
		machine = virtual_machine()

		self.configure(machine)

		machine.set_network_gateway.assert_called_once_with(True)
		network_service = self.network_service.return_value
		network_service.get_routes_with.assert_called_once_with(Route("2000::/3", "host"))
		network_service.update_network.assert_called_once_with(
			{"public_ipv6": "2001:db8::/64", "routes": network_service.get_routes_with.return_value}
		)

	# A retry after a later failure must not attach the block twice.
	def test_a_configured_vm_is_not_changed_again(self) -> None:
		machine = virtual_machine(is_network_gateway=1)
		machine.get_metal_vm_info.return_value = metal_information("151.115.112.94", "2001:db8::/64")

		self.configure(machine)

		machine.set_network_gateway.assert_not_called()
		self.network_service.return_value.update_network.assert_not_called()

	def test_a_vm_with_another_block_is_refused(self) -> None:
		machine = virtual_machine(is_network_gateway=1)
		machine.get_metal_vm_info.return_value = metal_information("151.115.112.94", "2001:db8:1::/64")

		with self.assertRaisesRegex(frappe.ValidationError, "holds IPv6 block"):
			self.configure(machine)

		self.network_service.return_value.update_network.assert_not_called()


class TestRouterInstallation(UnitTestCase):
	def install(self, result) -> Mock:
		provisioner = IPv6RouterServerProvisioner(router())
		task = SimpleNamespace(name="task-1", result=result)
		with (
			patch.object(
				provisioning.IPV6_ROUTER_PACKAGE.__class__,
				"get_install_environment",
				return_value=PACKAGE_ENVIRONMENT,
			),
			patch.object(provisioning.frappe, "get_single", return_value=SimpleNamespace(region_id=3)),
			patch.object(provisioning.SSHTask, "create_for_script_file", return_value=task) as create_task,
		):
			provisioner.install_router()
		return create_task

	def test_the_installer_receives_the_region_and_the_block(self) -> None:
		create_task = self.install(SimpleNamespace(is_success=True))

		arguments = create_task.call_args.kwargs
		self.assertEqual(arguments["script_path"], "install-service-package.sh")
		self.assertEqual(
			arguments["environment"],
			PACKAGE_ENVIRONMENT | {"REGION_ID": 3, "PUBLIC_IPV6_PREFIX": "2001:db8::/64"},
		)

	def test_a_failed_installation_names_its_task(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "task-1"):
			self.install(SimpleNamespace(is_success=False))
