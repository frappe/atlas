from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.vm.core.metal_client import MetalClientError
from atlas.vm.core.placement import PlacementService
from atlas.vm.core.vm_service import VirtualMachineCreateError, VirtualMachineService
from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage


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
	def test_creation_commits_the_draft_before_the_metal_request(self) -> None:
		operations: list[str] = []
		image = SimpleNamespace(
			name="image-1",
			platform="amd64",
			enabled=1,
			title="Ubuntu",
			validate_compatibility=Mock(),
		)
		server = SimpleNamespace(name="server-1")
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
			patch.object(PlacementService, "select_server", return_value=server),
			patch.object(VirtualMachineService, "insert_draft", return_value=virtual_machine),
			patch.object(VirtualMachineService, "get_metal_request", return_value={"request": True}),
			patch(
				"atlas.vm.core.vm_service.frappe.db.commit",
				side_effect=lambda: operations.append("commit"),
			),
			patch("atlas.vm.core.vm_service.MetalClient", return_value=metal_client),
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
			platform="amd64",
			enabled=1,
			title="Ubuntu",
			validate_compatibility=Mock(),
		)
		server = SimpleNamespace(name="server-1")
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
			patch.object(PlacementService, "select_server", return_value=server),
			patch.object(VirtualMachineService, "insert_draft", return_value=virtual_machine),
			patch.object(VirtualMachineService, "get_metal_request", return_value={"request": True}),
			patch("atlas.vm.core.vm_service.frappe.db.commit") as commit,
			patch("atlas.vm.core.vm_service.MetalClient", return_value=metal_client),
		):
			result = VirtualMachineService.create(self.request())

		self.assertEqual(result, {"name": "VM-00001", "is_draft": True})
		commit.assert_called_once()
		virtual_machine.save.assert_not_called()

	def test_confirmed_create_failure_identifies_the_committed_draft(self) -> None:
		image = SimpleNamespace(
			name="image-1",
			platform="amd64",
			enabled=1,
			title="Ubuntu",
			validate_compatibility=Mock(),
		)
		server = SimpleNamespace(name="server-1")
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
			patch.object(PlacementService, "select_server", return_value=server),
			patch.object(VirtualMachineService, "insert_draft", return_value=virtual_machine),
			patch.object(VirtualMachineService, "get_metal_request", return_value={"request": True}),
			patch("atlas.vm.core.vm_service.frappe.db.commit") as commit,
			patch("atlas.vm.core.vm_service.MetalClient", return_value=metal_client),
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
			"vcpus": 2,
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
			patch("atlas.vm.core.vm_service.MetalClient", return_value=metal_client),
		):
			information = VirtualMachineService(virtual_machine).get_information()

		self.assertIsNone(information)

	def test_metal_failure_is_visible_to_the_caller(self) -> None:
		virtual_machine = SimpleNamespace(name="VM-00001", server="server-1")
		metal_client = Mock()
		metal_client.get_virtual_machine.side_effect = MetalClientError("connection refused")

		with (
			patch("atlas.vm.core.vm_service.frappe.get_doc", return_value=Mock()),
			patch("atlas.vm.core.vm_service.MetalClient", return_value=metal_client),
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

	def test_a_smaller_disk_is_rejected(self) -> None:
		service, information = self.build_service()

		with (
			patch.object(service, "require_information", return_value=information),
			patch.object(service, "set_disk") as set_disk,
			self.assertRaises(frappe.ValidationError),
		):
			service.update_disk({"size_mib": 10240})

		set_disk.assert_not_called()


class TestVirtualMachineCompute(UnitTestCase):
	def build_service(self, observed_state: str = "running") -> tuple[VirtualMachineService, SimpleNamespace]:
		"""Return a service whose host reports one desired compute object."""
		virtual_machine = SimpleNamespace(name="VM-00001", sleep_after_idle_seconds=0, db_set=Mock())
		information = SimpleNamespace(
			desired=SimpleNamespace(
				compute=SimpleNamespace(
					virtual_cpu_count=2,
					memory_mib=2048,
					sleep_after_idle_seconds=0,
				)
			),
			observed=SimpleNamespace(state=observed_state),
		)
		return VirtualMachineService(virtual_machine), information

	def test_an_idle_timeout_change_does_not_need_a_stopped_virtual_machine(self) -> None:
		service, information = self.build_service("running")

		with (
			patch.object(service, "require_information", return_value=information),
			patch.object(service, "set_compute", return_value={}) as set_compute,
		):
			service.update_compute({"sleep_after_idle_seconds": 1800})

		set_compute.assert_called_once_with(
			{
				"virtual_cpu_count": 2,
				"memory_mib": 2048,
				"sleep_after_idle_seconds": 1800,
			}
		)
		service.virtual_machine.db_set.assert_called_once_with("sleep_after_idle_seconds", 1800)

	def test_a_shape_change_keeps_the_stored_idle_timeout(self) -> None:
		"""Metal replaces the complete compute object, so a resize must resend the timeout."""
		service, information = self.build_service("stopped")
		information.desired.compute.sleep_after_idle_seconds = 1800
		service.virtual_machine.sleep_after_idle_seconds = 1800

		with (
			patch.object(service, "require_information", return_value=information),
			patch.object(service, "set_compute", return_value={}) as set_compute,
		):
			service.update_compute({"virtual_cpu_count": 4})

		set_compute.assert_called_once_with(
			{
				"virtual_cpu_count": 4,
				"memory_mib": 2048,
				"sleep_after_idle_seconds": 1800,
			}
		)
		service.virtual_machine.db_set.assert_called_once_with({"vcpus": 4, "memory_mib": 2048})

	def test_a_shape_change_needs_a_stopped_virtual_machine(self) -> None:
		service, information = self.build_service("running")

		with (
			patch.object(service, "require_information", return_value=information),
			patch.object(service, "set_compute") as set_compute,
			self.assertRaises(frappe.ValidationError),
		):
			service.update_compute({"memory_mib": 4096})

		set_compute.assert_not_called()


class TestVirtualMachineNetworkChanges(UnitTestCase):
	def build_service(self, attached: str | None) -> VirtualMachineService:
		"""Return a service for a managed virtual machine."""
		virtual_machine = SimpleNamespace(name="VM-00001", validate_network_change=Mock())
		service = VirtualMachineService(virtual_machine)
		service.get_attached_ip_address_name = Mock(return_value=attached)
		return service

	def test_an_unknown_egress_mode_is_rejected(self) -> None:
		service = self.build_service(None)

		with patch.object(service, "update_network") as update_network:
			with self.assertRaises(frappe.ValidationError):
				service.apply_network_changes({"egress": "server"})

			update_network.assert_not_called()

	def test_an_attached_address_keeps_the_internet_path(self) -> None:
		service = self.build_service("203.0.113.10")

		for egress in ("mesh", "none"):
			with patch.object(service, "update_network") as update_network:
				with self.assertRaises(frappe.ValidationError):
					service.apply_network_changes({"egress": egress})

				update_network.assert_not_called()

	def test_a_limit_change_needs_no_address_check(self) -> None:
		service = self.build_service("203.0.113.10")

		with patch.object(service, "update_network", return_value={}) as update_network:
			service.apply_network_changes({"public_network_throughput_mibps": 25})

		update_network.assert_called_once_with({"public_network_throughput_mibps": 25})

	def test_an_ip_address_from_another_tenant_is_rejected(self) -> None:
		virtual_machine = SimpleNamespace(name="VM-00001", tenant_id=7, server="server-1")
		address = SimpleNamespace(tenant_id=8, status="Allocated", virtual_machine=None)

		with (
			patch("atlas.vm.core.vm_service.frappe.get_doc", return_value=address) as get_doc,
			self.assertRaises(frappe.PermissionError),
		):
			VirtualMachineService(virtual_machine).assign_ip_address("203.0.113.10")

		get_doc.assert_called_once_with("Metal Server IP Address", "203.0.113.10", for_update=True)
