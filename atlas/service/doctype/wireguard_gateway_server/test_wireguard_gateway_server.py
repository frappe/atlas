from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

import atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server as gateway_module
from atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server import (
	WireGuardGatewayServer,
)


def ipv4_allocation(**values) -> SimpleNamespace:
	return SimpleNamespace(
		**(
			{
				"version": "4",
				"status": "Reserved",
				"tenant_id": 0,
			}
			| values
		)
	)


class TestIPv4AllocationValidation(UnitTestCase):
	def validate(self, allocation: SimpleNamespace) -> None:
		with (
			patch.object(gateway_module.frappe.db, "exists", return_value=True),
			patch.object(gateway_module.frappe, "get_doc", return_value=allocation),
		):
			gateway_module._validate_ipv4_allocation("allocation-1")

	def test_a_tenant_zero_reservation_is_accepted(self) -> None:
		self.validate(ipv4_allocation())

	def test_an_unreserved_allocation_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "reserved by tenant 0"):
			self.validate(ipv4_allocation(status="Available"))


class TestListenPortValidation(UnitTestCase):
	def test_the_default_port_is_accepted(self) -> None:
		gateway_module._validate_listen_port(51820)

	def test_a_zero_port_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "Listen port"):
			gateway_module._validate_listen_port(0)

	def test_a_non_integer_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "Listen port"):
			gateway_module._validate_listen_port("51820")


class TestGatewayCreation(UnitTestCase):
	def test_gateway_stores_the_mesh_address_of_its_virtual_machine(self) -> None:
		gateway_server = SimpleNamespace()
		with patch.object(gateway_module, "get_virtual_machine_mesh_address", return_value="fdaa:1::99"):
			WireGuardGatewayServer._set_virtual_machine(gateway_server, "vm-00001")
		self.assertEqual(gateway_server.virtual_machine, "vm-00001")
		self.assertEqual(gateway_server.wireguard_mesh_ipv6, "fdaa:1::99")

	def test_pending_and_interrupted_provisioning_are_queued(self) -> None:
		gateway_server = MagicMock()
		with (
			patch.object(gateway_module.frappe, "get_all", side_effect=[["wg-gateway-001"], []]),
			patch.object(gateway_module.frappe, "get_doc", return_value=gateway_server),
		):
			gateway_module.enqueue_pending_gateway_provisioning()
		gateway_server.enqueue_provisioning.assert_called_once_with(enqueue_after_commit=False)

	def test_a_pending_gateway_without_a_virtual_machine_is_failed(self) -> None:
		with (
			patch.object(gateway_module.frappe, "get_all", side_effect=[[], ["wg-gateway-002"]]),
			patch.object(gateway_module.frappe.db, "set_value") as set_value,
		):
			gateway_module.enqueue_pending_gateway_provisioning()

		self.assertEqual(set_value.call_args.args[:2], ("WireGuard Gateway Server", "wg-gateway-002"))
		self.assertEqual(set_value.call_args.args[2]["status"], "Failed")

	def test_a_placement_failure_marks_the_gateway_failed(self) -> None:
		from atlas.vm.core.placement import PlacementBusy

		gateway = SimpleNamespace(name="wg-gateway-001", status="Pending", failure_message=None)
		values = {
			"virtual_machine_image": "image",
			"cpu_millicores": 2000,
			"memory_mib": 2048,
			"disk_mib": 8192,
			"public_ipv4": "allocation",
		}
		with (
			patch.object(
				gateway_module.frappe, "get_single", return_value=SimpleNamespace(public_ssh_key="ssh-key")
			),
			patch(
				"atlas.vm.core.vm_service.VirtualMachineService.create",
				side_effect=PlacementBusy("Every candidate Metal Server is busy."),
			),
		):
			is_draft = WireGuardGatewayServer._create_virtual_machine(gateway, values)

		self.assertFalse(is_draft)
		self.assertEqual(gateway.status, "Failed")
		self.assertTrue(gateway.failure_message.startswith("placement:"))
