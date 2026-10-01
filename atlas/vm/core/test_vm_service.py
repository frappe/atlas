import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core.exceptions import AtlasConflictError
from atlas.vm.core.metal_client import MetalClientError
from atlas.vm.core.models import Route, VirtualMachineCreateRequest
from atlas.vm.core.placement import OutOfCapacity, PlacementStrategy
from atlas.vm.core.vm_service import (
	InsufficientHostCapacity,
	VirtualMachineCreateError,
	VirtualMachineService,
)
from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage

AFFINITY_RULES = [{"resource": "virtual_machine", "operator": "has_not", "tags": {"role": "cargo-server"}}]


def build_image(tenant_id: int, image_type: str = "machine") -> VirtualMachineImage:
	"""Return one image document that answers the tenant visibility rule."""
	image = VirtualMachineImage.__new__(VirtualMachineImage)
	image.tenant_id = tenant_id
	image.image_type = image_type
	image.title = "Worker snapshot"
	image.enabled = 1
	image.validate_is_available = Mock()
	return image


class TestVirtualMachineCreation(UnitTestCase):
	def test_out_of_capacity_does_not_create_a_vm_draft(self) -> None:
		image = SimpleNamespace(architecture="amd64", validate_compatibility=Mock())
		with (
			patch.object(VirtualMachineService, "get_image", return_value=image),
			patch.object(PlacementStrategy, "find_server", side_effect=OutOfCapacity("retry later")),
			patch.object(VirtualMachineService, "insert_draft") as insert_draft,
			self.assertRaises(OutOfCapacity),
		):
			VirtualMachineService.create(self.request())

		insert_draft.assert_not_called()

	def test_any_tenant_can_send_placement_rules(self) -> None:
		image = SimpleNamespace(architecture="amd64", validate_compatibility=Mock())
		# Tenant 7, on a VM that is not privileged.
		request = {**self.request(), "placement_rules": AFFINITY_RULES}
		with (
			patch.object(VirtualMachineService, "get_image", return_value=image),
			patch.object(
				PlacementStrategy, "find_server", side_effect=OutOfCapacity("retry later")
			) as find_server,
			self.assertRaises(OutOfCapacity),
		):
			VirtualMachineService.create(request)

		self.assertEqual(find_server.call_args.args[0].placement_rules.as_list(), AFFINITY_RULES)

	def test_the_draft_stores_the_tags_and_placement_rules(self) -> None:
		image = SimpleNamespace(name="image-1", architecture="amd64")
		for placement_rules in (AFFINITY_RULES, []):
			request = VirtualMachineCreateRequest.from_value(
				{**self.request(), "tags": {"role": "cargo-server"}, "placement_rules": placement_rules}
			)
			with (
				self.subTest(placement_rules=placement_rules),
				patch("atlas.vm.core.vm_service.frappe.get_doc") as get_doc,
			):
				VirtualMachineService.insert_draft(request, image, "metal-1")

				values = get_doc.call_args.args[0]
				self.assertEqual(values["tags"], [{"key": "role", "value": "cargo-server"}])
				# A VM without rules stores nothing.
				stored_rules = values["placement_rules"]
				self.assertEqual(json.loads(stored_rules) if stored_rules else [], placement_rules)

	def test_creation_commits_the_draft_before_the_metal_request(self) -> None:
		operations: list[str] = []
		image = SimpleNamespace(
			name="image-1",
			architecture="amd64",
			enabled=1,
			title="Ubuntu",
			validate_compatibility=Mock(),
		)
		virtual_machine = SimpleNamespace(
			name="VM-00001",
			flags=SimpleNamespace(),
			is_draft=1,
			save=Mock(side_effect=lambda **arguments: operations.append("save")),
		)
		metal_client = Mock()
		metal_client.put_virtual_machine.side_effect = lambda *arguments: operations.append("metal")

		with (
			patch.object(VirtualMachineService, "get_image", return_value=image),
			patch.object(PlacementStrategy, "find_server", return_value="metal-1"),
			patch.object(VirtualMachineService, "insert_draft", return_value=virtual_machine),
			patch.object(VirtualMachineService, "get_metal_request", return_value={"request": True}),
			patch(
				"atlas.vm.core.vm_service.frappe.db.commit",
				side_effect=lambda: operations.append("commit"),
			),
			patch.object(VirtualMachineService, "metal_client", metal_client),
		):
			result = VirtualMachineService.create(self.request())

		self.assertEqual(result, {"name": "VM-00001", "is_draft": False})
		self.assertEqual(operations, ["commit", "metal", "save"])
		self.assertEqual(virtual_machine.is_draft, 0)

	def test_a_system_image_can_boot_for_any_tenant(self) -> None:
		image = build_image(tenant_id=0, image_type="system")

		with patch("atlas.vm.core.vm_service.frappe.get_doc", return_value=image):
			self.assertIs(VirtualMachineService.get_image("system-image", 7), image)

		image.validate_is_available.assert_called_once()

	def test_another_tenant_machine_image_cannot_boot(self) -> None:
		image = build_image(tenant_id=8)

		with (
			patch("atlas.vm.core.vm_service.frappe.get_doc", return_value=image),
			self.assertRaises(frappe.DoesNotExistError),
		):
			VirtualMachineService.get_image("machine-image", 7)

	def test_uncertain_create_keeps_the_committed_draft(self) -> None:
		image = SimpleNamespace(
			name="image-1",
			architecture="amd64",
			enabled=1,
			title="Ubuntu",
			validate_compatibility=Mock(),
		)
		virtual_machine = SimpleNamespace(
			name="VM-00001",
			flags=SimpleNamespace(),
			is_draft=1,
			save=Mock(),
		)
		metal_client = Mock()
		metal_client.put_virtual_machine.side_effect = MetalClientError("lost", uncertain=True)

		with (
			patch.object(VirtualMachineService, "get_image", return_value=image),
			patch.object(PlacementStrategy, "find_server", return_value="metal-1"),
			patch.object(VirtualMachineService, "insert_draft", return_value=virtual_machine),
			patch.object(VirtualMachineService, "get_metal_request", return_value={"request": True}),
			patch("atlas.vm.core.vm_service.frappe.db.commit") as commit,
			patch.object(VirtualMachineService, "metal_client", metal_client),
		):
			result = VirtualMachineService.create(self.request())

		self.assertEqual(result, {"name": "VM-00001", "is_draft": True})
		commit.assert_called_once()
		virtual_machine.save.assert_not_called()

	def test_confirmed_create_failure_identifies_the_committed_draft(self) -> None:
		image = SimpleNamespace(
			name="image-1",
			architecture="amd64",
			enabled=1,
			title="Ubuntu",
			validate_compatibility=Mock(),
		)
		virtual_machine = SimpleNamespace(
			name="VM-00001",
			flags=SimpleNamespace(),
			is_draft=1,
			save=Mock(),
		)
		metal_client = Mock()
		metal_client.put_virtual_machine.side_effect = MetalClientError("rejected", status=400)

		with (
			patch.object(VirtualMachineService, "get_image", return_value=image),
			patch.object(PlacementStrategy, "find_server", return_value="metal-1"),
			patch.object(VirtualMachineService, "insert_draft", return_value=virtual_machine),
			patch.object(VirtualMachineService, "get_metal_request", return_value={"request": True}),
			patch("atlas.vm.core.vm_service.frappe.db.commit") as commit,
			patch.object(VirtualMachineService, "metal_client", metal_client),
			self.assertRaises(VirtualMachineCreateError) as raised,
		):
			VirtualMachineService.create(self.request())

		self.assertEqual(raised.exception.virtual_machine_name, "VM-00001")
		commit.assert_called_once()
		virtual_machine.save.assert_not_called()

	@staticmethod
	def request() -> dict[str, int | str]:
		return {
			"virtual_machine_image": "image-1",
			"cpu_millicores": 2000,
			"memory_mib": 2048,
			"disk_mib": 10240,
			"tenant_id": 7,
		}


class TestVirtualMachineInformation(UnitTestCase):
	def test_missing_virtual_machine_returns_no_information(self) -> None:
		virtual_machine = SimpleNamespace(name="VM-00001", server="server-1")
		metal_client = Mock()
		metal_client.get_virtual_machine.side_effect = MetalClientError("not found", status=404)

		with (
			patch("atlas.vm.core.vm_service.frappe.get_doc", return_value=Mock()),
			patch.object(VirtualMachineService, "metal_client", metal_client),
		):
			information = VirtualMachineService(virtual_machine).get_information()

		self.assertIsNone(information)

	def test_metal_failure_is_visible_to_the_caller(self) -> None:
		virtual_machine = SimpleNamespace(name="VM-00001", server="server-1")
		metal_client = Mock()
		metal_client.get_virtual_machine.side_effect = MetalClientError("connection refused")

		with (
			patch("atlas.vm.core.vm_service.frappe.get_doc", return_value=Mock()),
			patch.object(VirtualMachineService, "metal_client", metal_client),
			self.assertRaisesRegex(frappe.ValidationError, "connection refused"),
		):
			VirtualMachineService(virtual_machine).get_information()


class TestVirtualMachineDisk(UnitTestCase):
	def build_service(self, size_mib: int = 20480) -> tuple[VirtualMachineService, Mock]:
		"""Return a service whose host reports one desired disk."""
		virtual_machine = SimpleNamespace(name="VM-00001", sleep_after_idle_seconds=0, db_set=Mock())
		service = VirtualMachineService(virtual_machine)
		information = SimpleNamespace(
			desired=SimpleNamespace(disk=SimpleNamespace(size_mib=size_mib, throughput_mibps=50, iops=2000))
		)
		return service, information

	def test_a_limit_change_keeps_the_stored_disk_size(self) -> None:
		service, information = self.build_service()

		with (
			patch.object(service, "require_information", return_value=information),
			patch.object(service, "set_disk", return_value={}) as set_disk,
		):
			service.update_disk({"throughput_mibps": 100})

		set_disk.assert_called_once_with(20480, 100, 2000)
		service.virtual_machine.db_set.assert_not_called()

	def test_a_larger_disk_updates_the_stored_size(self) -> None:
		service, information = self.build_service()

		with (
			patch.object(service, "require_information", return_value=information),
			patch.object(service, "set_disk", return_value={}) as set_disk,
		):
			service.update_disk({"size_mib": 40960})

		set_disk.assert_called_once_with(40960, 50, 2000)
		service.virtual_machine.db_set.assert_called_once_with("disk_mib", 40960)

	def test_a_full_host_tells_the_caller_to_stop_and_resize(self) -> None:
		service, information = self.build_service()
		metal_client = Mock()
		metal_client.set_virtual_machine_disk.side_effect = MetalClientError(
			"no room", status=409, code="insufficient_capacity"
		)

		with (
			patch.object(service, "require_information", return_value=information),
			patch.object(VirtualMachineService, "metal_client", metal_client),
			self.assertRaisesRegex(InsufficientHostCapacity, "Stop the Virtual Machine"),
		):
			service.update_disk({"size_mib": 40960})

		service.virtual_machine.db_set.assert_not_called()

	def test_a_smaller_disk_is_rejected(self) -> None:
		service, information = self.build_service()

		with (
			patch.object(service, "require_information", return_value=information),
			patch.object(service, "set_disk") as set_disk,
			self.assertRaises(frappe.ValidationError),
		):
			service.update_disk({"size_mib": 10240})

		set_disk.assert_not_called()


class TestVirtualMachineNetworkChanges(UnitTestCase):
	def build_service(self, attached: str | None) -> VirtualMachineService:
		"""Return a service for a managed virtual machine."""
		virtual_machine = SimpleNamespace(
			name="VM-00001", validate_network_change=Mock(), is_network_gateway=0
		)
		service = VirtualMachineService(virtual_machine)
		service.get_ipv4_address_name = Mock(return_value=attached)
		service.get_public_ipv6 = Mock(return_value="")
		return service

	def test_termination_detaches_all_public_ip_allocations(self) -> None:
		virtual_machine = SimpleNamespace(name="VM-00001", db_set=Mock())
		service = VirtualMachineService(virtual_machine)
		allocations = {"allocation-4": Mock(), "allocation-6": Mock()}

		with (
			patch.object(VirtualMachineService, "metal_client", Mock()),
			patch("atlas.vm.core.vm_service.frappe.get_all", return_value=list(allocations)),
			patch(
				"atlas.vm.core.vm_service.frappe.get_doc",
				side_effect=lambda _doctype, name: allocations[name],
			),
		):
			service.terminate()

		for allocation in allocations.values():
			allocation.begin_detach.assert_called_once_with()

	# An allocation job can run before Metal stores a new VM. An empty route list
	# would then replace the create routes, so the read must fail and retry.
	def test_a_route_edit_fails_until_metal_holds_the_vm(self) -> None:
		service = self.build_service(None)

		with (
			patch.object(service, "get_information", return_value=None),
			patch.object(service, "require_information", side_effect=frappe.ValidationError("not found")),
			self.assertRaises(frappe.ValidationError),
		):
			service.get_routes_with(Route("2000::/3", "fdaa:1::56"))

	def test_ipv4_internet_access_adds_and_removes_only_the_ipv4_host_route(self) -> None:
		service = self.build_service(None)
		gateway_route = Route("2000::/3", "fdaa:1::56")

		for enabled, routes in (
			(True, [gateway_route]),
			(False, [gateway_route, Route("0.0.0.0/0", "host")]),
		):
			with (
				patch.object(service, "get_routes", return_value=routes),
				patch.object(service, "update_network", return_value={}) as update_network,
			):
				service.apply_network_changes({"ipv4_internet_access": enabled})

			sent = update_network.call_args.args[0]["routes"]
			self.assertIn(gateway_route.as_dict(), sent)
			self.assertEqual({"destination": "0.0.0.0/0", "via": "host"} in sent, enabled)

	def test_an_invalid_route_is_rejected(self) -> None:
		service = self.build_service(None)

		with patch.object(service, "update_network") as update_network:
			with self.assertRaises(frappe.ValidationError):
				service.apply_network_changes({"routes": [{"destination": "0.0.0.0/0", "via": "server"}]})

			update_network.assert_not_called()

	def test_an_attached_ipv4_address_keeps_its_host_route(self) -> None:
		service = self.build_service("203.0.113.10")

		for routes in ([], [{"destination": "10.0.0.0/8", "via": "host"}]):
			with patch.object(service, "update_network") as update_network:
				with self.assertRaisesRegex(frappe.ValidationError, "Detach the public IPv4"):
					service.apply_network_changes({"routes": routes})

				update_network.assert_not_called()

	def test_ipv4_internet_access_stays_on_while_a_public_ipv4_is_attached(self) -> None:
		service = self.build_service("203.0.113.10")

		with (
			patch.object(service, "get_routes", return_value=[Route("0.0.0.0/0", "host")]),
			patch.object(service, "update_network") as update_network,
			self.assertRaises(AtlasConflictError),
		):
			service.apply_network_changes({"ipv4_internet_access": False})

		update_network.assert_not_called()

	def test_a_limit_change_needs_no_address_check(self) -> None:
		service = self.build_service("203.0.113.10")

		with patch.object(service, "update_network", return_value={}) as update_network:
			service.apply_network_changes({"public_network_throughput_mibps": 25})

		update_network.assert_called_once_with({"public_network_throughput_mibps": 25})

	def test_a_route_edit_preserves_the_wireguard_gateway_routes(self) -> None:
		service = self.build_service(None)
		gateway_route = Route("fdac:1:1::/48", "fdaa:1::1", "wireguard-gateway")

		with (
			patch.object(service, "get_routes", return_value=[Route("0.0.0.0/0", "host"), gateway_route]),
			patch.object(service, "update_network", return_value={}) as update_network,
		):
			service.set_routes([{"destination": "2000::/3", "via": "host"}])

		update_network.assert_called_once_with(
			{
				"routes": [
					# The edit replaces the normal route list and keeps the gateway routes.
					{"destination": "2000::/3", "via": "host"},
					{"destination": "fdac:1:1::/48", "via": "fdaa:1::1", "scope": "wireguard-gateway"},
				]
			}
		)

	def test_the_route_editor_skips_the_wireguard_gateway_routes(self) -> None:
		service = self.build_service(None)
		gateway_route = Route("fdac:1:1::/48", "fdaa:1::1", "wireguard-gateway")

		with (
			patch("atlas.vm.core.vm_service.frappe.get_all", return_value=[]),
			patch.object(service, "get_routes", return_value=[Route("0.0.0.0/0", "host"), gateway_route]),
		):
			rows = service.get_route_editor_rows()

		self.assertEqual(rows, [{"destination": "0.0.0.0/0", "via": "host", "gateway_virtual_machine": ""}])

	# The gateway routes belong to the VM namespace on the host, so an edit of
	# the normal routes never removes or replaces them.
	def test_sync_gateway_routes_replaces_only_the_wireguard_gateway_routes(self) -> None:
		service = self.build_service(None)
		stale = Route("fdac:1:2::/48", "fdaa:1::2", "wireguard-gateway")
		wanted = Route("fdac:1:1::/48", "fdaa:1::1", "wireguard-gateway")

		with (
			patch.object(service, "get_routes", return_value=[Route("2000::/3", "host"), stale]),
			patch.object(service, "update_network", return_value={}) as update_network,
		):
			service.sync_gateway_routes([wanted])

		update_network.assert_called_once_with(
			{
				"routes": [
					{"destination": "2000::/3", "via": "host"},
					{"destination": "fdac:1:1::/48", "via": "fdaa:1::1", "scope": "wireguard-gateway"},
				]
			}
		)

	def test_sync_gateway_routes_skips_an_unchanged_set(self) -> None:
		service = self.build_service(None)
		current = Route("fdac:1:1::/48", "fdaa:1::1", "wireguard-gateway")

		with (
			patch.object(service, "get_routes", return_value=[Route("2000::/3", "host"), current]),
			patch.object(service, "update_network", return_value={}) as update_network,
		):
			service.sync_gateway_routes([current])

		update_network.assert_not_called()

	def test_set_wg_gateway_accessible_installs_the_active_gateway_routes(self) -> None:
		service = self.build_service(None)
		wanted = Route("fdac:1:1::/48", "fdaa:1::1", "wireguard-gateway")

		with (
			patch(
				"atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server.active_gateway_routes",
				return_value=[wanted],
			),
			patch.object(service, "sync_gateway_routes") as sync_gateway_routes,
		):
			service.set_wg_gateway_accessible(True)

		sync_gateway_routes.assert_called_once_with([wanted])

	def test_set_wg_gateway_accessible_without_an_active_gateway_fails(self) -> None:
		service = self.build_service(None)

		with (
			patch(
				"atlas.service.doctype.wireguard_gateway_server.wireguard_gateway_server.active_gateway_routes",
				return_value=[],
			),
			patch.object(service, "sync_gateway_routes") as sync_gateway_routes,
			self.assertRaisesRegex(frappe.ValidationError, "Active WireGuard Gateway Server"),
		):
			service.set_wg_gateway_accessible(True)

		sync_gateway_routes.assert_not_called()

	def test_set_wg_gateway_accessible_removes_the_gateway_routes(self) -> None:
		service = self.build_service(None)

		with patch.object(service, "sync_gateway_routes") as sync_gateway_routes:
			service.set_wg_gateway_accessible(False)

		sync_gateway_routes.assert_called_once_with([])

	def test_has_gateway_routes_ignores_the_wireguard_gateway_routes(self) -> None:
		service = self.build_service(None)
		gateway_route = Route("fdac:1:1::/48", "fdaa:1::1", "wireguard-gateway")

		for routes, expected in (
			([gateway_route], False),
			([gateway_route, Route("2000::/3", "fdaa:1::56")], True),
		):
			with patch.object(service, "get_routes", return_value=routes):
				self.assertEqual(service.has_gateway_routes(), expected)
