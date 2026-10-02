from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

import atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server as gateway_module
from atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server import (
	WireguardGatewayServer,
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


class TestGatewayRoutes(UnitTestCase):
	def test_each_active_gateway_owns_one_return_route(self) -> None:
		gateways = [
			SimpleNamespace(name="wireguard-001", wireguard_mesh_ipv6="fdaa:1::1"),
			SimpleNamespace(name="wireguard-007", wireguard_mesh_ipv6="fdaa:1::7"),
		]

		with (
			patch.object(gateway_module.frappe, "get_single", return_value=SimpleNamespace(region_id=1)),
			patch.object(gateway_module.frappe, "get_all", return_value=gateways),
		):
			routes = gateway_module.get_wireguard_gateway_routes()

		self.assertEqual(
			routes,
			[
				{"destination": "fdac:1:1::/48", "via": "fdaa:1::1"},
				{"destination": "fdac:1:7::/48", "via": "fdaa:1::7"},
			],
		)

	def test_the_firewall_admits_tunnels_the_api_and_the_region_mesh_only(self) -> None:
		inbound = gateway_module.get_gateway_firewall(1, 51820)["inbound"]

		self.assertIn({"protocol": "udp", "ports": "51820", "cidrs": gateway_module.ANYWHERE}, inbound)
		self.assertIn({"protocol": "any", "cidrs": ["fdaa:1::/32"]}, inbound)
		self.assertEqual([rule["ports"] for rule in inbound if rule["protocol"] == "tcp"], ["443"])

	def test_the_client_prefix_embeds_the_region_and_the_gateway_id(self) -> None:
		self.assertEqual(gateway_module.gateway_client_prefix(1, "wireguard-001"), "fdac:1:1::/48")
		self.assertEqual(gateway_module.gateway_client_prefix(2, "wireguard-007"), "fdac:2:7::/48")


class TestGatewayCreation(UnitTestCase):
	def test_a_new_node_gets_its_own_key_pair(self) -> None:
		gateway_server = WireguardGatewayServer.__new__(WireguardGatewayServer)
		gateway_server.flags = frappe._dict(created_by_wg_gateway_api=True)

		WireguardGatewayServer.before_insert(gateway_server)

		self.assertEqual(
			gateway_module.get_public_key(gateway_server.private_key), gateway_server.gateway_public_key
		)

	def test_gateway_stores_the_mesh_address_of_its_virtual_machine(self) -> None:
		gateway_server = SimpleNamespace()
		with patch.object(gateway_module, "get_virtual_machine_mesh_address", return_value="fdaa:1::99"):
			WireguardGatewayServer._set_virtual_machine(gateway_server, "vm-00001")
		self.assertEqual(gateway_server.virtual_machine, "vm-00001")
		self.assertEqual(gateway_server.wireguard_mesh_ipv6, "fdaa:1::99")

	def test_pending_and_interrupted_provisioning_are_queued(self) -> None:
		gateway_server = MagicMock()
		with (
			patch.object(gateway_module.frappe, "get_all", side_effect=[["wireguard-001"], []]),
			patch.object(gateway_module, "now_datetime", return_value=datetime(2026, 10, 2)),
			patch.object(gateway_module.frappe, "get_doc", return_value=gateway_server),
		):
			gateway_module.enqueue_pending_gateway_provisioning()
		gateway_server.enqueue_provisioning.assert_called_once_with(enqueue_after_commit=False)

	def test_a_pending_gateway_without_a_virtual_machine_is_failed(self) -> None:
		with (
			patch.object(gateway_module.frappe, "get_all", side_effect=[[], ["wireguard-002"]]),
			patch.object(gateway_module, "now_datetime", return_value=datetime(2026, 10, 2)),
			patch.object(gateway_module.frappe.db, "set_value") as set_value,
		):
			gateway_module.enqueue_pending_gateway_provisioning()

		self.assertEqual(set_value.call_args.args[:2], ("Wireguard Gateway Server", "wireguard-002"))
		self.assertEqual(set_value.call_args.args[2]["status"], "Failed")

	def test_a_placement_failure_marks_the_gateway_failed(self) -> None:
		from atlas.vm.core.placement import PlacementBusy

		gateway = SimpleNamespace(name="wireguard-001", status="Pending", failure_message=None)
		values = {
			"virtual_machine_image": "image",
			"cpu_millicores": 2000,
			"memory_mib": 2048,
			"disk_mib": 8192,
			"public_ipv4": "allocation",
		}
		with (
			patch.object(
				gateway_module.frappe,
				"get_single",
				return_value=SimpleNamespace(public_ssh_key="ssh-key", region_id=1),
			),
			patch(
				"atlas.vm.core.vm_service.VirtualMachineService.create",
				side_effect=PlacementBusy("Every candidate Metal Server is busy."),
			),
		):
			is_draft = WireguardGatewayServer._create_virtual_machine(gateway, values)

		self.assertFalse(is_draft)
		self.assertEqual(gateway.status, "Failed")
		self.assertTrue(gateway.failure_message.startswith("placement:"))


SHAPE = {"virtual_machine_image": "ubuntu", "cpu_millicores": 2000, "memory_mib": 2048, "disk_mib": 8192}


class TestGatewayRebuild(UnitTestCase):
	def rebuild(self, status: str = "Active") -> tuple[MagicMock, MagicMock, list[str]]:
		order: list[str] = []
		gateway_server = MagicMock(status=status, listen_port=51820)
		gateway_server.get.side_effect = SHAPE.get
		gateway_server.leave_cluster.side_effect = lambda action: order.append(f"leave {action}")
		gateway_server.terminate_virtual_machine.side_effect = lambda: order.append("terminate")
		gateway_server._create_virtual_machine.side_effect = lambda values: order.append("create")

		with (
			patch.object(gateway_module, "_validate_system_manager"),
			patch.object(gateway_module, "_validate_create_request") as validate,
			patch.object(gateway_module.frappe, "get_doc", return_value=gateway_server),
			patch.object(gateway_module.frappe, "msgprint"),
			patch.object(gateway_module, "filelock"),
		):
			WireguardGatewayServer.rebuild(
				SimpleNamespace(doctype="Wireguard Gateway Server", name="wireguard-002"), "allocation-2"
			)
		return gateway_server, validate, order

	def test_the_new_machine_takes_the_recorded_shape_and_the_new_address(self) -> None:
		gateway_server, validate, order = self.rebuild()

		self.assertEqual(
			validate.call_args.args[0], SHAPE | {"public_ipv4": "allocation-2", "listen_port": 51820}
		)
		self.assertEqual(order, ["leave rebuild", "terminate", "create"])
		self.assertEqual(gateway_server.update.call_args.args[0]["pushed_config_hash"], None)
		gateway_server.enqueue_provisioning.assert_called_once_with()

	def test_an_archived_node_is_not_rebuilt(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "archived"):
			self.rebuild(status="Archived")

	def test_a_terminating_or_missing_machine_is_not_terminated_again(self) -> None:
		gateway_server = SimpleNamespace(virtual_machine="vm-00001")
		machine = MagicMock(is_terminating=1)
		with (
			patch.object(gateway_module.frappe.db, "exists", return_value=True),
			patch.object(gateway_module.frappe, "get_doc", return_value=machine),
		):
			WireguardGatewayServer.terminate_virtual_machine(gateway_server)
		machine.terminate.assert_not_called()

		with patch.object(gateway_module.frappe.db, "exists", return_value=False):
			WireguardGatewayServer.terminate_virtual_machine(gateway_server)

	def test_leaving_drains_the_node_before_the_other_nodes_drop_it(self) -> None:
		order: list[str] = []
		gateway_server = MagicMock(status="Active", is_cluster_member=1, dns_health_check_id="check-1")
		gateway_server.name = "wireguard-002"
		gateway_server.save.side_effect = lambda **kwargs: order.append("save")
		gateway_server.remove_dns_records.side_effect = lambda: order.append("dns")

		with (
			patch.object(gateway_module.frappe.db, "commit", side_effect=lambda: order.append("commit")),
			patch("atlas.service.core.wg_gateway.provisioning.WireGuardGatewayProvisioner") as provisioner,
			patch(
				"atlas.service.core.wg_gateway.provisioning.apply_membership_to_active_gateways",
				side_effect=lambda excluding: order.append("membership"),
			) as apply_membership,
		):
			provisioner.return_value.stop_api.side_effect = lambda: order.append("stop api")
			WireguardGatewayServer.leave_cluster(gateway_server, "archive")

		gateway_server.update.assert_called_once_with(
			{"status": "Failed", "failure_message": "archive: in progress", "is_cluster_member": 0}
		)
		apply_membership.assert_called_once_with(excluding="wireguard-002")
		self.assertEqual(order, ["save", "commit", "dns", "save", "commit", "stop api", "membership"])
		self.assertIsNone(gateway_server.dns_health_check_id)
