from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from atlas.vm.core.metal_client import MetalClientError
from atlas.vm.core.models import VirtualMachineShape
from atlas.vm.core.placement import CurrentPlacement
from atlas.vm.core.vm_resize import VirtualMachineResize
from atlas.vm.core.vm_service import MetalOperationError, VirtualMachineService


def build_information(state: str = "stopped") -> SimpleNamespace:
	"""Return one Metal record with a 2 CPU, 2 GiB, 20 GiB shape."""
	return SimpleNamespace(
		desired=SimpleNamespace(
			compute=SimpleNamespace(cpu_millicores=2000, memory_mib=2048, sleep_after_idle_seconds=0),
			disk=SimpleNamespace(size_mib=20480, throughput_mibps=50, iops=2000),
		),
		observed=SimpleNamespace(state=state),
	)


class TestVirtualMachineResize(UnitTestCase):
	def setUp(self) -> None:
		self.virtual_machine = SimpleNamespace(
			name="VM-00001",
			server="metal-1",
			architecture="amd64",
			tenant_id=7,
			cpu_millicores=2000,
			memory_mib=2048,
			disk_mib=20480,
			sleep_after_idle_seconds=0,
			db_set=Mock(),
		)
		self.resize = VirtualMachineResize(self.virtual_machine)
		self.metal_client = self.start_patch(patch.object(VirtualMachineService, "metal_client", Mock()))
		self.find_server = self.start_patch(
			patch("atlas.vm.core.vm_resize.PlacementStrategy.find_server", return_value="metal-1")
		)
		self.create_migration = self.start_patch(
			patch("atlas.vm.core.vm_resize.MigrationService.create", return_value="mig-00001")
		)

	def start_patch(self, active_patch) -> Mock:
		mock = active_patch.start()
		self.addCleanup(active_patch.stop)
		return mock

	def apply(self, changes: dict[str, int], state: str = "stopped") -> str | None:
		with patch.object(self.resize.service, "require_information", return_value=build_information(state)):
			return self.resize.apply(changes)

	def test_an_idle_change_alone_stays_in_place_on_a_running_virtual_machine(self) -> None:
		self.assertIsNone(self.apply({"sleep_after_idle_seconds": 1800}, state="running"))

		self.metal_client.set_virtual_machine_compute.assert_called_once_with(
			"VM-00001", {"cpu_millicores": 2000, "memory_mib": 2048, "sleep_after_idle_seconds": 1800}
		)
		self.virtual_machine.db_set.assert_called_once_with("sleep_after_idle_seconds", 1800)
		self.find_server.assert_not_called()

	def test_a_retry_repairs_stale_stored_resources(self) -> None:
		self.virtual_machine.memory_mib = 2048
		information = build_information()
		information.desired.compute.memory_mib = 4096

		with patch.object(self.resize.service, "require_information", return_value=information):
			self.resize.apply({"memory_mib": 4096})

		self.virtual_machine.db_set.assert_called_once_with(
			{
				"cpu_millicores": 2000,
				"memory_mib": 4096,
				"disk_mib": 20480,
				"sleep_after_idle_seconds": 0,
			}
		)

	def test_a_retry_repairs_a_stale_idle_shutdown_value(self) -> None:
		information = build_information()
		information.desired.compute.sleep_after_idle_seconds = 600

		with patch.object(self.resize.service, "require_information", return_value=information):
			self.resize.apply({"sleep_after_idle_seconds": 600})

		self.virtual_machine.db_set.assert_called_once_with(
			{
				"cpu_millicores": 2000,
				"memory_mib": 2048,
				"disk_mib": 20480,
				"sleep_after_idle_seconds": 600,
			}
		)

	def test_a_resource_change_needs_a_stopped_virtual_machine(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "Stop the Virtual Machine"):
			self.apply({"memory_mib": 4096}, state="running")

		self.find_server.assert_not_called()
		self.metal_client.set_virtual_machine_compute.assert_not_called()

	def test_the_current_host_places_the_increase(self) -> None:
		self.apply({"memory_mib": 4096, "sleep_after_idle_seconds": 600})

		requirements = self.find_server.call_args.args[0]
		self.assertEqual(
			(requirements.cpu_millicores, requirements.memory_mib, requirements.disk_mib), (2000, 4096, 20480)
		)
		self.assertTrue(requirements.is_sleepy)
		self.assertEqual(
			self.find_server.call_args.kwargs["current_placement"],
			CurrentPlacement("metal-1", memory_mib=2048, disk_mib=20480),
		)

	def test_a_fitting_host_sets_the_complete_shape(self) -> None:
		self.assertIsNone(self.apply({"cpu_millicores": 4000, "disk_mib": 40960}))

		self.metal_client.resize_virtual_machine.assert_called_once_with(
			"VM-00001",
			{"cpu_millicores": 4000, "memory_mib": 2048, "disk_mib": 40960, "sleep_after_idle_seconds": 0},
		)
		self.virtual_machine.db_set.assert_called_once_with(
			{"cpu_millicores": 4000, "memory_mib": 2048, "disk_mib": 40960, "sleep_after_idle_seconds": 0}
		)
		self.create_migration.assert_not_called()

	def test_another_host_starts_a_resize_migration(self) -> None:
		self.find_server.return_value = "metal-2"

		self.assertEqual(self.apply({"memory_mib": 8192}), "mig-00001")

		self.create_migration.assert_called_once_with(
			self.virtual_machine,
			destination_metal_server="metal-2",
			resize=VirtualMachineShape(2000, 8192, 20480, 0),
		)
		self.metal_client.set_virtual_machine_compute.assert_not_called()

	def test_a_move_applies_the_idle_change_on_the_source_first(self) -> None:
		self.find_server.return_value = "metal-2"

		self.apply({"memory_mib": 8192, "sleep_after_idle_seconds": 600})

		self.metal_client.set_virtual_machine_compute.assert_called_once_with(
			"VM-00001", {"cpu_millicores": 2000, "memory_mib": 2048, "sleep_after_idle_seconds": 600}
		)
		self.create_migration.assert_called_once()

	def test_a_full_current_host_falls_back_to_a_migration(self) -> None:
		self.metal_client.resize_virtual_machine.side_effect = MetalClientError(
			"no room", status=409, code="insufficient_capacity"
		)

		self.assertEqual(self.apply({"memory_mib": 8192}), "mig-00001")

		self.virtual_machine.db_set.assert_not_called()

	def test_another_metal_error_does_not_start_a_migration(self) -> None:
		self.metal_client.resize_virtual_machine.side_effect = MetalClientError("boom", status=500)

		with self.assertRaises(MetalOperationError):
			self.apply({"memory_mib": 8192})

		self.create_migration.assert_not_called()

	def test_a_smaller_disk_is_rejected(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "only increase"):
			self.apply({"disk_mib": 10240})

		self.find_server.assert_not_called()

	def test_an_invalid_value_is_rejected_before_a_host_read(self) -> None:
		for resize_values in (
			{"cpu_millicores": 99},
			{"memory_mib": 0},
			{"disk_mib": 0},
			{"gpu_count": 1},
		):
			with (
				patch.object(self.resize.service, "require_information") as require_information,
				self.assertRaises(frappe.ValidationError),
			):
				self.resize.apply(resize_values)

			require_information.assert_not_called()
