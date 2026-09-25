from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from frappe.tests import UnitTestCase

import atlas.metal_server.core.public_ip_service as service_module
from atlas.metal_server.core.public_ip_service import PublicIPService
from atlas.vm.core.models import Route
from atlas.vm.core.vm_service import VirtualMachineService


def pool(name: str, gateway: str):
	return SimpleNamespace(name=name, gateway=gateway, prefix="2001:db8::/64", is_routed=True)


class TestAllocationReplenishment(UnitTestCase):
	def test_only_static_direct_pools_are_replenished(self) -> None:
		public_pool = SimpleNamespace(name="pool-1", enabled=1, is_routed=False, source="Static")
		with (
			patch.object(service_module.frappe, "get_all", return_value=["pool-1"]) as get_all,
			patch.object(service_module.frappe, "get_doc", return_value=public_pool),
			patch.object(service_module.frappe.db, "count", return_value=0),
			patch.object(service_module, "_generate_available_allocations") as generate,
		):
			service_module.replenish_direct_allocations()

		get_all.assert_called_once_with(
			"Public IP Pool",
			filters={"enabled": 1, "gateway": ["is", "not set"], "source": "Static"},
			pluck="name",
		)
		generate.assert_called_once_with(public_pool, service_module.ALLOCATION_BATCH_SIZE)

	def test_pool_is_replenished_after_eighty_percent_is_consumed(self) -> None:
		public_pool = SimpleNamespace(name="pool-1", enabled=1, is_routed=False, source="Static")
		with (
			patch.object(service_module.frappe, "get_all", return_value=[public_pool.name]),
			patch.object(service_module.frappe, "get_doc", return_value=public_pool),
			patch.object(
				service_module.frappe.db,
				"count",
				return_value=service_module.ALLOCATION_REPLENISH_THRESHOLD,
			),
			patch.object(service_module, "_generate_available_allocations", return_value=1) as generate,
		):
			service_module.replenish_direct_allocations()

		generate.assert_called_once_with(public_pool, service_module.ALLOCATION_BATCH_SIZE)

	def test_pool_is_not_replenished_above_the_threshold(self) -> None:
		public_pool = SimpleNamespace(name="pool-1", enabled=1, is_routed=False, source="Static")
		with (
			patch.object(service_module.frappe, "get_all", return_value=[public_pool.name]),
			patch.object(service_module.frappe, "get_doc", return_value=public_pool),
			patch.object(
				service_module.frappe.db,
				"count",
				return_value=service_module.ALLOCATION_REPLENISH_THRESHOLD + 1,
			),
			patch.object(service_module, "_generate_available_allocations") as generate,
		):
			service_module.replenish_direct_allocations()

		generate.assert_not_called()


class TestGatewayPoolSelection(UnitTestCase):
	def test_a_gateway_on_the_vm_server_is_preferred(self) -> None:
		pools = {
			"local-pool": pool("local-pool", "local-router"),
			"remote-pool": pool("remote-pool", "remote-router"),
		}

		def router_value(_doctype, gateway, *_args, **_kwargs):
			server = "metal-1" if gateway == "local-router" else "metal-2"
			return SimpleNamespace(status="Active", server=server)

		with (
			patch.object(service_module.frappe, "get_all", return_value=list(pools)),
			patch.object(service_module.frappe, "get_doc", side_effect=lambda _doctype, name: pools[name]),
			patch.object(service_module.frappe.db, "get_value", side_effect=router_value),
			patch.object(service_module.random, "choice", side_effect=lambda values: values[0]),
		):
			selected = PublicIPService()._select_routed_pool(SimpleNamespace(server="metal-1"))

		self.assertEqual(selected.name, "local-pool")

	def test_a_remote_gateway_is_used_when_no_local_gateway_exists(self) -> None:
		remote = pool("remote-pool", "remote-router")
		with (
			patch.object(service_module.frappe, "get_all", return_value=[remote.name]),
			patch.object(service_module.frappe, "get_doc", return_value=remote),
			patch.object(
				service_module.frappe.db,
				"get_value",
				return_value=SimpleNamespace(status="Active", server="metal-2"),
			),
		):
			selected = PublicIPService()._select_routed_pool(SimpleNamespace(server="metal-1"))

		self.assertEqual(selected.name, "remote-pool")


class TestAllocationAssignment(UnitTestCase):
	def test_named_reservation_is_validated_after_its_row_is_locked(self) -> None:
		allocation = SimpleNamespace(
			tenant_id=7,
			version="4",
			is_reserved=0,
			is_routed=False,
		)
		with patch.object(service_module.frappe, "get_doc", return_value=allocation) as get_doc:
			with self.assertRaises(service_module.PublicIPNotFound):
				PublicIPService()._owned_reserved_allocation("allocation-1", 7, 4)

		get_doc.assert_called_once_with("Public IP Allocation", "allocation-1", for_update=True)

	def test_attach_locks_the_vm_before_checking_existing_allocations(self) -> None:
		virtual_machine = SimpleNamespace(
			name="vm-1",
			is_draft=False,
			tenant_id=7,
			server="metal-1",
			ensure_not_migrating=Mock(),
			validate_network_change=Mock(),
		)
		allocation = SimpleNamespace(
			name="32eb57bc-9548-4a89-8358-543e26883569",
			tenant_id=7,
			version="4",
			is_reserved=1,
			is_routed=False,
			status="Reserved",
			virtual_machine=None,
			begin_attach=Mock(),
		)

		def get_doc(doctype, _name, **kwargs):
			self.assertTrue(kwargs.get("for_update"))
			return virtual_machine if doctype == "Virtual Machine" else allocation

		with (
			patch.object(service_module.frappe, "get_doc", side_effect=get_doc),
			patch.object(service_module.frappe.db, "get_value", return_value=None),
			patch.object(VirtualMachineService, "get_routes", return_value=[Route("0.0.0.0/0", "host")]),
		):
			result = PublicIPService().attach(virtual_machine, 4, allocation.name)

		self.assertIs(result, allocation)
		allocation.begin_attach.assert_called_once_with("vm-1", "metal-1", 7)

	def test_ipv4_attach_needs_ipv4_internet_access(self) -> None:
		virtual_machine = SimpleNamespace(
			name="vm-1", is_draft=False, ensure_not_migrating=Mock(), validate_network_change=Mock()
		)
		with (
			patch.object(service_module.frappe, "get_doc", return_value=virtual_machine),
			patch.object(VirtualMachineService, "get_routes", return_value=[]),
			self.assertRaises(service_module.IPv4InternetAccessRequired),
		):
			PublicIPService().attach(virtual_machine, 4, "auto")


UNRELATED_ROUTE = Route("2001:db8:ffff::/48", "fdaa:1::2")


def direct_pool(version: str):
	return SimpleNamespace(
		name="pool-1", gateway=None, is_routed=False, source="Static", version=version, host_address=None
	)


class TestRouteAttachment(UnitTestCase):
	def attach(self, public_pool, prefix: str, routes: list[Route]) -> MagicMock:
		router = SimpleNamespace(wireguard_mesh_ipv6="fdaa:1::1")
		with (
			patch.object(service_module.frappe, "get_doc", return_value=router),
			patch.object(VirtualMachineService, "get_routes", return_value=routes),
			patch.object(VirtualMachineService, "update_network") as update_network,
		):
			PublicIPService()._apply_attach(public_pool, SimpleNamespace(name="vm-1"), prefix, "metal-1")
		return update_network

	def detach(self, public_pool, routes: list[Route], information: object = True) -> MagicMock:
		with (
			patch.object(VirtualMachineService, "get_information", return_value=information),
			patch.object(VirtualMachineService, "get_routes", return_value=routes),
			patch.object(VirtualMachineService, "update_network") as update_network,
		):
			PublicIPService()._apply_detach(public_pool, SimpleNamespace(name="vm-1", is_terminating=0))
		return update_network

	def test_routed_attach_replaces_the_internet_route_and_keeps_other_routes(self) -> None:
		update_network = self.attach(
			pool("pool-1", "router-1"), "2001:db8::1/128", [UNRELATED_ROUTE, Route("2000::/3", "fdaa:1::9")]
		)

		update_network.assert_called_once_with(
			{"routes": [UNRELATED_ROUTE.as_dict(), {"destination": "2000::/3", "via": "fdaa:1::1"}]}
		)

	def test_direct_ipv6_attach_sets_the_address_and_its_host_route(self) -> None:
		update_network = self.attach(direct_pool("6"), "2001:db8::7/128", [UNRELATED_ROUTE])

		update_network.assert_called_once_with(
			{
				"public_ipv6": "2001:db8::7/128",
				"routes": [UNRELATED_ROUTE.as_dict(), {"destination": "2000::/3", "via": "host"}],
			}
		)

	def test_direct_ipv4_attach_sets_the_address_and_its_host_route(self) -> None:
		update_network = self.attach(direct_pool("4"), "203.0.113.7/32", [])

		update_network.assert_called_once_with(
			{"public_ipv4": "203.0.113.7", "routes": [{"destination": "0.0.0.0/0", "via": "host"}]}
		)

	def test_routed_detach_removes_only_the_internet_route(self) -> None:
		update_network = self.detach(
			pool("pool-1", "router-1"), [UNRELATED_ROUTE, Route("2000::/3", "fdaa:1::1")]
		)

		update_network.assert_called_once_with({"routes": [UNRELATED_ROUTE.as_dict()]})

	def test_direct_ipv6_detach_removes_the_address_and_its_host_route(self) -> None:
		update_network = self.detach(direct_pool("6"), [UNRELATED_ROUTE, Route("2000::/3", "host")])

		update_network.assert_called_once_with({"public_ipv6": "", "routes": [UNRELATED_ROUTE.as_dict()]})

	def test_detach_skips_a_vm_that_metal_does_not_hold(self) -> None:
		update_network = self.detach(direct_pool("6"), [], information=None)

		update_network.assert_not_called()


class TestIntentApplication(UnitTestCase):
	def test_attach_waits_while_metal_has_not_created_the_draft(self) -> None:
		intent = SimpleNamespace(
			status="Attaching", virtual_machine="vm-1", server="metal-1", pool="pool-1", version=1
		)
		draft = SimpleNamespace(name="vm-1", is_draft=1)
		with (
			patch.object(service_module.frappe, "get_doc", side_effect=[direct_pool("6"), draft]),
			patch.object(VirtualMachineService, "get_information", return_value=None),
			patch.object(PublicIPService, "_apply_attach") as apply_attach,
			patch.object(PublicIPService, "_complete_attach") as complete_attach,
		):
			PublicIPService().apply_intent(SimpleNamespace(name="allocation-1"), intent)

		apply_attach.assert_not_called()
		complete_attach.assert_not_called()
