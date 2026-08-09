"""`provision.resize_capacity` — Central's pre-resize capacity read.

Unlike `capacity()` (the best host's free headroom for a NEW machine), a resize
reshapes a VM in place on the host it already occupies. So the ceiling is THAT
host's free room with the VM's own footprint added back — the VM can always keep
its size or shrink, and grow into whatever else the host has spare. These tests pin
that contract and the add-back arithmetic.
"""

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.api import provision
from atlas.tests.fixtures import make_image, make_provider, make_server, make_virtual_machine


def _clean_virtual_machines() -> None:
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


class TestProvisionResizeCapacity(IntegrationTestCase):
	def setUp(self) -> None:
		_clean_virtual_machines()
		frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 1)
		# Isolate the resize add-back arithmetic from the host memory reserve so the
		# ceilings below are the host's raw totals (the reserve has its own coverage in
		# test_api_server_capacity) — parallels the overprovision_factor neutralisation.
		frappe.db.set_single_value("Atlas Settings", "host_memory_reserve_megabytes", 0)
		self.provider = make_provider("resize-capacity-provider")
		for name in frappe.get_all("Server", filters={"status": "Active"}, pluck="name"):
			frappe.db.set_value("Server", name, "status", "Draining")
		self.image = make_image("resize-capacity-image")

	def _active_server(self, **totals):
		server = make_server(
			self.provider,
			"resize-capacity-server",
			size="DigitalOcean/s-4vcpu-8gb",
			ipv6_address="2001:db8:9::1",
			ipv6_prefix="2001:db8:9::/64",
			ipv6_virtual_machine_range="2001:db8:9::/124",
			status="Active",
		)
		server.db_set("vcpus_total", 0)
		server.db_set("memory_megabytes_total", 0)
		server.db_set("pool_disk_gigabytes_total", 0)
		for field, value in totals.items():
			server.db_set(field, value)
		return server

	def test_contract_shape(self) -> None:
		server = self._active_server(
			vcpus_total=4, memory_megabytes_total=8192, pool_disk_gigabytes_total=160
		)
		vm = make_virtual_machine(
			server, self.image, vcpus=1, cpu_max_cores=1, memory_megabytes=2048, disk_gigabytes=40
		)
		result = provision.resize_capacity(vm.name)
		self.assertEqual(set(result), {"available", "unmeasured", "largest_vm"})
		self.assertEqual(set(result["largest_vm"]), {"vcpus", "memory_megabytes", "disk_gigabytes"})

	def test_lone_vm_can_grow_to_the_whole_host(self) -> None:
		# The only VM on the host: its ceiling is the host's full totals (its own footprint
		# freed and re-reserved), so it can grow to fill the box.
		server = self._active_server(
			vcpus_total=4, memory_megabytes_total=8192, pool_disk_gigabytes_total=160
		)
		vm = make_virtual_machine(
			server, self.image, vcpus=1, cpu_max_cores=1, memory_megabytes=2048, disk_gigabytes=40
		)
		result = provision.resize_capacity(vm.name)
		self.assertTrue(result["available"])
		self.assertFalse(result["unmeasured"])
		self.assertEqual(result["largest_vm"], {"vcpus": 4, "memory_megabytes": 8192, "disk_gigabytes": 160})

	def test_shares_host_with_a_neighbour(self) -> None:
		# Host 4 / 8192 / 160; VM-A (resizing) 1 / 2048 / 40, neighbour VM-B 1 / 1024 / 20.
		# VM-A's ceiling adds back only its OWN footprint: cpu 4-2+1=3, mem 8192-3072+2048=7168,
		# disk 160-60+40=140 — the neighbour's reservation still stands.
		server = self._active_server(
			vcpus_total=4, memory_megabytes_total=8192, pool_disk_gigabytes_total=160
		)
		vm_a = make_virtual_machine(
			server, self.image, vcpus=1, cpu_max_cores=1, memory_megabytes=2048, disk_gigabytes=40
		)
		make_virtual_machine(
			server, self.image, vcpus=1, cpu_max_cores=1, memory_megabytes=1024, disk_gigabytes=20
		)
		result = provision.resize_capacity(vm_a.name)
		self.assertEqual(result["largest_vm"], {"vcpus": 3, "memory_megabytes": 7168, "disk_gigabytes": 140})

	def test_unmeasured_host_returns_sentinels(self) -> None:
		# No agent totals and an uncatalogued size → every axis uncatalogued → unlimited.
		server = self._active_server()
		server.db_set("size", "Scaleway/EM-B130E-NVME")  # not in the CPU slug dict
		vm = make_virtual_machine(
			server, self.image, vcpus=1, cpu_max_cores=1, memory_megabytes=2048, disk_gigabytes=40
		)
		result = provision.resize_capacity(vm.name)
		self.assertTrue(result["available"])
		self.assertTrue(result["unmeasured"])
		self.assertGreaterEqual(result["largest_vm"]["vcpus"], 1024)

	def test_unknown_vm_is_unavailable(self) -> None:
		result = provision.resize_capacity("no-such-vm")
		self.assertFalse(result["available"])
		self.assertIsNone(result["largest_vm"])
