"""`provision.capacity` — Central's pre-create capacity read.

Central speaks in resources (CPU / RAM / disk), not Atlas size presets, and never
sees hosts. So `capacity()` returns `{available, unmeasured, largest_vm}` — no
preset ladder, no per-server breakdown. These tests pin that contract and that
`largest_vm` is a real co-schedulable shape (the free headroom on the best host),
with a large sentinel + `unmeasured` flag when the host's totals aren't reported.
"""

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.api import provision
from atlas.tests.fixtures import make_image, make_provider, make_server, make_virtual_machine


def _clean_virtual_machines() -> None:
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


class TestProvisionCapacity(IntegrationTestCase):
	def setUp(self) -> None:
		_clean_virtual_machines()
		frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 1)
		self.provider = make_provider("provision-capacity-provider")
		# Neutralize any Active server other suites left so this test's own
		# server is the only placement candidate.
		for name in frappe.get_all("Server", filters={"status": "Active"}, pluck="name"):
			frappe.db.set_value("Server", name, "status", "Draining")
		self.image = make_image("provision-capacity-image")

	def _active_server(self, **totals):
		server = make_server(
			self.provider,
			"provision-capacity-server",
			size="DigitalOcean/s-4vcpu-8gb",
			ipv6_address="2001:db8:7::1",
			ipv6_prefix="2001:db8:7::/64",
			ipv6_virtual_machine_range="2001:db8:7::/124",
			status="Active",
		)
		# Reset every agent-reported total first, then apply this test's — the row
		# is reused by title across tests, so stale totals would leak.
		server.db_set("vcpus_total", 0)
		server.db_set("memory_megabytes_total", 0)
		server.db_set("pool_disk_gigabytes_total", 0)
		for field, value in totals.items():
			server.db_set(field, value)
		return server

	# --- contract shape ----------------------------------------------------

	def test_contract_has_no_sizes_or_servers(self) -> None:
		self._active_server()
		result = provision.capacity()
		self.assertEqual(set(result), {"available", "unmeasured", "largest_vm"})
		self.assertEqual(set(result["largest_vm"]), {"vcpus", "memory_megabytes", "disk_gigabytes"})

	# --- unmeasured host → sentinel shape ---------------------------------

	def test_unmeasured_axes_return_sentinels(self) -> None:
		# RAM and disk have no reported total (and no slug fallback) → sentinels;
		# CPU is catalogued via the size slug (s-4vcpu-8gb → 4), so it's a real
		# number even though the shape is flagged unmeasured. Report what we know,
		# sentinel only what we don't.
		self._active_server()  # size s-4vcpu-8gb, no agent totals
		result = provision.capacity()
		self.assertTrue(result["available"], "operator-vouched Active host can seat a VM")
		self.assertTrue(result["unmeasured"], "some axis unreported → shape is a placeholder")
		self.assertEqual(result["largest_vm"]["vcpus"], 4, "CPU catalogued via slug — real")
		self.assertGreaterEqual(result["largest_vm"]["memory_megabytes"], 1024 * 1024)
		self.assertGreaterEqual(result["largest_vm"]["disk_gigabytes"], 1024 * 1024)

	def test_fully_uncatalogued_host_all_sentinels(self) -> None:
		# A host with an unknown size slug too → every axis is a sentinel.
		server = self._active_server()
		server.db_set("size", "Scaleway/EM-B130E-NVME")  # not in the CPU slug dict
		result = provision.capacity()
		self.assertTrue(result["unmeasured"])
		self.assertGreaterEqual(result["largest_vm"]["vcpus"], 1024)
		self.assertGreaterEqual(result["largest_vm"]["memory_megabytes"], 1024 * 1024)
		self.assertGreaterEqual(result["largest_vm"]["disk_gigabytes"], 1024 * 1024)

	# --- measured host → free headroom ------------------------------------

	def test_measured_host_reports_free_headroom(self) -> None:
		# Fully catalogued host with one VM on it: largest_vm is effective - used
		# per axis, and unmeasured is False.
		server = self._active_server(
			vcpus_total=4, memory_megabytes_total=8192, pool_disk_gigabytes_total=160
		)
		make_virtual_machine(
			server, self.image, vcpus=1, cpu_max_cores=1, memory_megabytes=2048, disk_gigabytes=40
		)
		result = provision.capacity()
		self.assertFalse(result["unmeasured"], "all axes reported → measured")
		self.assertEqual(result["largest_vm"]["vcpus"], 3)  # 4 - 1
		self.assertEqual(result["largest_vm"]["memory_megabytes"], 6144)  # 8192 - 2048
		self.assertEqual(result["largest_vm"]["disk_gigabytes"], 120)  # 160 - 40

	def test_measured_full_host_unavailable(self) -> None:
		# A measured host with no room left: smallest VM doesn't fit → not
		# available, and largest_vm is a zero shape (not a sentinel).
		server = self._active_server(vcpus_total=1, memory_megabytes_total=512, pool_disk_gigabytes_total=10)
		make_virtual_machine(
			server, self.image, vcpus=1, cpu_max_cores=1, memory_megabytes=512, disk_gigabytes=10
		)
		result = provision.capacity()
		self.assertFalse(result["available"])
		self.assertFalse(result["unmeasured"])
		self.assertEqual(result["largest_vm"]["memory_megabytes"], 0)
		self.assertEqual(result["largest_vm"]["disk_gigabytes"], 0)

	# --- no host at all ---------------------------------------------------

	def test_no_active_server_null_shape(self) -> None:
		# setUp drained every Active server and created none.
		result = provision.capacity()
		self.assertFalse(result["available"])
		self.assertFalse(result["unmeasured"])
		self.assertIsNone(result["largest_vm"])

	# --- best host wins ---------------------------------------------------

	def test_largest_vm_picks_host_with_most_free(self) -> None:
		# Two measured hosts; the roomier one defines largest_vm.
		small = self._active_server(vcpus_total=2, memory_megabytes_total=2048, pool_disk_gigabytes_total=40)
		# _active_server reuses one row by title; make a distinct second host.
		big = make_server(
			self.provider,
			"provision-capacity-server-big",
			size="DigitalOcean/s-8vcpu-16gb",
			ipv6_address="2001:db8:8::1",
			ipv6_prefix="2001:db8:8::/64",
			ipv6_virtual_machine_range="2001:db8:8::/124",
			status="Active",
		)
		big.db_set("vcpus_total", 8)
		big.db_set("memory_megabytes_total", 16384)
		big.db_set("pool_disk_gigabytes_total", 320)
		# Leave both empty; big has strictly more free on every axis.
		result = provision.capacity()
		self.assertFalse(result["unmeasured"])
		self.assertEqual(result["largest_vm"]["vcpus"], 8)
		self.assertEqual(result["largest_vm"]["memory_megabytes"], 16384)
		self.assertEqual(result["largest_vm"]["disk_gigabytes"], 320)
		_ = small  # placement never surfaces the loser to Central

	def test_measured_host_wins_over_unmeasured(self) -> None:
		# Mixed fleet: a modest MEASURED host and an unmeasured one. The measured
		# host must define largest_vm — otherwise the unmeasured host's giant
		# sentinels would always win and hide a real, trustworthy shape.
		measured = self._active_server(
			vcpus_total=2, memory_megabytes_total=2048, pool_disk_gigabytes_total=40
		)
		unmeasured = make_server(
			self.provider,
			"provision-capacity-unmeasured",
			ipv6_address="2001:db8:8::1",
			ipv6_prefix="2001:db8:8::/64",
			ipv6_virtual_machine_range="2001:db8:8::/124",
			status="Active",
		)
		# db_set past the Link check to an uncatalogued slug → no CPU fallback, no
		# agent totals → all axes sentinel.
		unmeasured.db_set("size", "s-unknown-slug")
		result = provision.capacity()
		self.assertFalse(result["unmeasured"], "a measured host exists → trust it")
		self.assertEqual(result["largest_vm"]["vcpus"], 2)
		self.assertEqual(result["largest_vm"]["memory_megabytes"], 2048)
		self.assertEqual(result["largest_vm"]["disk_gigabytes"], 40)
		_ = measured
