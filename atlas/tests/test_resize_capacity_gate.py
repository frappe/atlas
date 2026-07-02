"""`VirtualMachine.resize()` capacity gate (`placement.ensure_resize_capacity`).

resize() reshapes a VM in place on its current host and — before this — had NO
capacity check, so an oversized grow silently over-committed the host and failed at
boot. The gate now caps a grow to the host's real free room (the VM's own footprint
freed, so downsizes / no-ops always pass) and fails fast with NoCapacityError instead.
run_task is mocked throughout — these pin the gate, not the on-host reshape.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module
from atlas.tests._mocks import fake_task
from atlas.tests.fixtures import make_image, make_provider, make_server, make_virtual_machine


def _clean_virtual_machines() -> None:
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


class TestResizeCapacityGate(IntegrationTestCase):
	def setUp(self) -> None:
		_clean_virtual_machines()
		frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 1)
		self.provider = make_provider("resize-gate-provider")
		for name in frappe.get_all("Server", filters={"status": "Active"}, pluck="name"):
			frappe.db.set_value("Server", name, "status", "Draining")
		self.image = make_image("resize-gate-image")

	def _host(self, **totals):
		server = make_server(
			self.provider,
			"resize-gate-server",
			size="DigitalOcean/s-4vcpu-8gb",
			ipv6_address="2001:db8:a::1",
			ipv6_prefix="2001:db8:a::/64",
			ipv6_virtual_machine_range="2001:db8:a::/124",
			status="Active",
		)
		server.db_set("vcpus_total", 0)
		server.db_set("memory_megabytes_total", 0)
		server.db_set("pool_disk_gigabytes_total", 0)
		for field, value in totals.items():
			server.db_set(field, value)
		return server

	def _stopped_vm(self, server, **shape):
		vm = make_virtual_machine(server, self.image, **shape)
		vm.db_set("status", "Stopped")
		vm.reload()
		return vm

	def test_grow_beyond_host_is_rejected_before_host_work(self) -> None:
		host = self._host(vcpus_total=4, memory_megabytes_total=8192, pool_disk_gigabytes_total=160)
		vm = self._stopped_vm(host, vcpus=1, cpu_max_cores=1, memory_megabytes=2048, disk_gigabytes=40)
		with patch.object(vm_module, "run_task") as run:
			with self.assertRaises(frappe.ValidationError) as raised:
				vm.resize(memory_megabytes=16384)  # host only has 8192
		self.assertIn("capacity", str(raised.exception).lower())
		run.assert_not_called()  # the gate throws before any on-host reshape

	def test_lone_vm_can_grow_into_the_whole_host(self) -> None:
		host = self._host(vcpus_total=4, memory_megabytes_total=8192, pool_disk_gigabytes_total=160)
		vm = self._stopped_vm(host, vcpus=1, cpu_max_cores=1, memory_megabytes=2048, disk_gigabytes=40)
		with patch.object(vm_module, "run_task", return_value=fake_task(name="task-resize")):
			vm.resize(memory_megabytes=8192)  # its own 2048 freed + 6144 spare
		vm.reload()
		self.assertEqual(vm.memory_megabytes, 8192)

	def test_downsize_always_passes(self) -> None:
		host = self._host(vcpus_total=4, memory_megabytes_total=8192, pool_disk_gigabytes_total=160)
		vm = self._stopped_vm(host, vcpus=2, cpu_max_cores=2, memory_megabytes=4096, disk_gigabytes=40)
		with patch.object(vm_module, "run_task", return_value=fake_task(name="task-resize")):
			vm.resize(memory_megabytes=2048)
		vm.reload()
		self.assertEqual(vm.memory_megabytes, 2048)

	def test_neighbour_reservation_caps_the_grow(self) -> None:
		# Host 8192; neighbour holds 4096, this VM holds 2048 → it can grow to 8192-4096 =
		# 4096 (its own footprint freed), not beyond — the neighbour's claim still stands.
		host = self._host(vcpus_total=4, memory_megabytes_total=8192, pool_disk_gigabytes_total=160)
		vm = self._stopped_vm(host, vcpus=1, cpu_max_cores=1, memory_megabytes=2048, disk_gigabytes=40)
		make_virtual_machine(host, self.image, vcpus=1, cpu_max_cores=1, memory_megabytes=4096, disk_gigabytes=20)
		with patch.object(vm_module, "run_task") as run:
			with self.assertRaises(frappe.ValidationError):
				vm.resize(memory_megabytes=6000)
		run.assert_not_called()
		with patch.object(vm_module, "run_task", return_value=fake_task(name="task-resize")):
			vm.resize(memory_megabytes=4096)  # exactly fills the host
		vm.reload()
		self.assertEqual(vm.memory_megabytes, 4096)

	def test_uncatalogued_host_allows_any_grow(self) -> None:
		# No agent totals → memory axis uncatalogued → unlimited (operator vouched by Active).
		host = self._host()
		vm = self._stopped_vm(host, vcpus=1, cpu_max_cores=1, memory_megabytes=2048, disk_gigabytes=40)
		with patch.object(vm_module, "run_task", return_value=fake_task(name="task-resize")):
			vm.resize(memory_megabytes=65536)
		vm.reload()
		self.assertEqual(vm.memory_megabytes, 65536)
