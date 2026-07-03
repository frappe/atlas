import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.api import server_capacity
from atlas.tests.fixtures import make_image, make_provider, make_server, make_virtual_machine


def _clean_virtual_machines() -> None:
	for name in frappe.get_all("Virtual Machine", pluck="name"):
		frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)


class TestServerCapacity(IntegrationTestCase):
	def setUp(self) -> None:
		_clean_virtual_machines()
		# Reset to the no-oversubscription default so other suites can't leak a
		# factor into these assertions; restore is unnecessary (1 is the default).
		frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 1)
		# Default to no memory floor so the raw-total assertions below stay clean;
		# the reserve tests opt back in explicitly.
		frappe.db.set_single_value("Atlas Settings", "host_memory_reserve_megabytes", 0)
		self.provider = make_provider("capacity-test-provider")
		self.server = make_server(
			self.provider,
			"capacity-test-server",
			ipv4_address="10.0.0.7",
			ipv6_address="2001:db8:9::1",
			ipv6_prefix="2001:db8:9::/64",
			ipv6_virtual_machine_range="2001:db8:9::/124",
			status="Active",
		)
		# `size` is read_only on the doctype JSON; bypass the field-level guard
		# via db_set so we can pin the slug for the CPU-fallback tests.
		self.server.db_set("size", "s-2vcpu-4gb-intel")
		# make_server reuses the row by title across tests, so a value stamped by
		# one test leaks into the next — reset every agent-reported total in setUp.
		# CPU falls back to the slug dict when vcpus_total is 0.
		self.server.db_set("vcpus_total", 0)
		# Agent-reported RAM/disk totals — the axes that have no slug fallback.
		self.server.db_set("memory_megabytes_total", 4096)
		self.server.db_set("pool_disk_gigabytes_total", 100)
		self.image = make_image("capacity-test-image")

	# --- CPU axis: slug fallback + oversubscription -------------------------

	def test_cpu_total_from_size_slug(self) -> None:
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["cpu"]["total"], 2)
		self.assertEqual(result["cpu"]["used"], 0)
		self.assertEqual(result["virtual_machine_count"], 0)

	def test_cpu_total_prefers_agent_reported_over_slug(self) -> None:
		# When the agent stamps vcpus_total it wins over the legacy slug dict.
		self.server.db_set("vcpus_total", 8)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["cpu"]["total"], 8)

	def test_cpu_effective_equals_total_at_default_factor(self) -> None:
		# Default factor is 1 → effective budget is the physical total.
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["cpu"]["effective"], 2)

	def test_cpu_effective_scales_with_overprovision_factor(self) -> None:
		frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 16)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["cpu"]["total"], 2, "physical total is unchanged")
		self.assertEqual(result["cpu"]["effective"], 32, "budget is total x factor")

	def test_cpu_used_sums_non_terminated_vms(self) -> None:
		# Whole-core VMs: cpu_max_cores defaults to vcpus, so the bandwidth sum
		# equals the vcpu sum (1 + 2 = 3); the terminated 4-vCPU VM is excluded.
		make_virtual_machine(self.server, self.image, vcpus=1)
		make_virtual_machine(self.server, self.image, vcpus=2)
		terminated = make_virtual_machine(self.server, self.image, vcpus=4)
		terminated.db_set("status", "Terminated")

		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["cpu"]["used"], 3)
		self.assertEqual(result["virtual_machine_count"], 2)

	def test_cpu_used_sums_bandwidth_not_thread_count(self) -> None:
		# Fractional sizes cost their bandwidth cap, not a whole vCPU each: four
		# 1/16-vCPU VMs (each vcpus=1, cpu_max_cores=0.0625) cost 0.25 vCPU of
		# budget, not 4.
		for _ in range(4):
			make_virtual_machine(self.server, self.image, vcpus=1, cpu_max_cores=0.0625)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertAlmostEqual(result["cpu"]["used"], 0.25)
		self.assertEqual(result["virtual_machine_count"], 4)

	# --- RAM and disk axes: hard fit, no oversubscription ------------------

	def test_memory_total_and_no_oversubscription(self) -> None:
		frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 16)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["memory"]["total"], 4096)
		self.assertEqual(result["memory"]["effective"], 4096, "RAM effective is the raw total — no factor")

	def test_memory_used_sums_non_terminated_vms(self) -> None:
		make_virtual_machine(self.server, self.image, memory_megabytes=512)
		make_virtual_machine(self.server, self.image, memory_megabytes=1024)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["memory"]["used"], 1536)

	def test_disk_total_and_no_oversubscription(self) -> None:
		frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 16)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["disk"]["total"], 100)
		self.assertEqual(result["disk"]["effective"], 100, "disk effective is the raw total — no factor")

	def test_disk_used_sums_root_plus_data(self) -> None:
		# Reserved disk is root + data, summed across non-Terminated VMs.
		make_virtual_machine(self.server, self.image, disk_gigabytes=10, data_disk_gigabytes=5)
		make_virtual_machine(self.server, self.image, disk_gigabytes=20)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["disk"]["used"], 35)

	# --- Memory floor (host_memory_reserve_megabytes) ---------------------

	def test_memory_reserve_subtracted_from_effective(self) -> None:
		# effective = total − reserve; the raw total stays physical for display.
		frappe.db.set_single_value("Atlas Settings", "host_memory_reserve_megabytes", 1024)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["memory"]["total"], 4096, "raw total unchanged")
		self.assertEqual(result["memory"]["effective"], 3072, "budget is total − reserve")

	def test_memory_reserve_clamps_at_zero(self) -> None:
		# A reserve larger than the host's RAM clamps the budget at 0, never negative.
		frappe.db.set_single_value("Atlas Settings", "host_memory_reserve_megabytes", 8192)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["memory"]["effective"], 0)

	# --- Share units + stranded (spec/28) ---------------------------------

	def test_share_units_and_stranded_worked_example(self) -> None:
		# The spec/28 worked example: an 8-core / 16 GB / 320 GB host at factor 1 with
		# a 1 GB memory reserve holds 30 share units (RAM binds), and the unused CPU
		# and disk the RAM axis can't sell are stranded.
		frappe.db.set_single_value("Atlas Settings", "host_memory_reserve_megabytes", 1024)
		self.server.db_set("vcpus_total", 8)
		self.server.db_set("memory_megabytes_total", 16384)
		self.server.db_set("pool_disk_gigabytes_total", 320)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["share_units"], {"total": 30, "used": 0, "free": 30})
		self.assertAlmostEqual(result["stranded"]["cpu"], 6.125)  # 8 − 30×0.0625
		self.assertEqual(result["stranded"]["memory"], 0)  # RAM is the binding axis
		self.assertEqual(result["stranded"]["disk"], 20)  # 320 − 30×10

	def test_share_units_used_and_free(self) -> None:
		# A Dedicated 1x (16 units on every axis) spends 16 of the 30 units.
		frappe.db.set_single_value("Atlas Settings", "host_memory_reserve_megabytes", 1024)
		self.server.db_set("vcpus_total", 8)
		self.server.db_set("memory_megabytes_total", 16384)
		self.server.db_set("pool_disk_gigabytes_total", 320)
		make_virtual_machine(
			self.server, self.image, vcpus=1, cpu_max_cores=1, memory_megabytes=8192, disk_gigabytes=160
		)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["share_units"], {"total": 30, "used": 16, "free": 14})

	def test_share_units_over_measured_axes_only(self) -> None:
		# Partial measurement: only RAM is catalogued (unknown slug → no CPU, disk
		# unset), so units count RAM alone and stranded has only the RAM axis.
		self.server.db_set("size", "s-unknown-slug")
		self.server.db_set("memory_megabytes_total", 4096)
		self.server.db_set("pool_disk_gigabytes_total", 0)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["share_units"]["total"], 8)  # 4096 / 512
		self.assertEqual(set(result["stranded"]), {"memory"})

	def test_share_units_none_when_fully_uncatalogued(self) -> None:
		# No axis measured → nothing to count share units against.
		self.server.db_set("size", "s-unknown-slug")
		self.server.db_set("memory_megabytes_total", 0)
		self.server.db_set("pool_disk_gigabytes_total", 0)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertIsNone(result["share_units"])
		self.assertIsNone(result["stranded"])

	# --- Uncatalogued axes → unlimited ------------------------------------

	def test_unset_memory_disk_totals_are_unlimited(self) -> None:
		# Before the agent reports RAM/disk, those axes are uncatalogued.
		self.server.db_set("memory_megabytes_total", 0)
		self.server.db_set("pool_disk_gigabytes_total", 0)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertIsNone(result["memory"]["effective"], "unset RAM total → unlimited")
		self.assertIsNone(result["disk"]["effective"], "unset disk total → unlimited")

	def test_unknown_cpu_size_returns_none_total(self) -> None:
		self.server.db_set("size", "s-unknown-slug")
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertIsNone(result["cpu"]["total"])
		self.assertIsNone(result["cpu"]["effective"], "unknown size has unlimited CPU budget")

	def test_unknown_cpu_size_unlimited_even_with_factor(self) -> None:
		# An uncatalogued size stays unlimited regardless of the multiplier —
		# there is no physical total to scale.
		frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 16)
		self.server.db_set("size", "s-unknown-slug")
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertIsNone(result["cpu"]["effective"])

	def test_missing_size_returns_none_cpu_total(self) -> None:
		self.server.db_set("size", None)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertIsNone(result["cpu"]["total"])
		self.assertIsNone(result["cpu"]["effective"])
		self.assertIsNone(result["size"])

	# --- Cluster roll-up ---------------------------------------------------

	def test_cluster_sums_each_axis(self) -> None:
		make_virtual_machine(self.server, self.image, vcpus=1, memory_megabytes=512, disk_gigabytes=10)
		cluster = server_capacity.cluster_capacity()
		# This server is the only Active one under this fixture, but other suites
		# may leave Active servers behind; assert our axis usage is included.
		self.assertGreaterEqual(cluster["cpu"]["used"], 1)
		self.assertGreaterEqual(cluster["memory"]["used"], 512)
		self.assertGreaterEqual(cluster["disk"]["used"], 10)
		self.assertIn("uncatalogued", cluster["cpu"])

	def test_cluster_rolls_up_share_units(self) -> None:
		# The fleet view carries a share-unit roll-up across measured hosts (this
		# fixture host is fully measured, so it contributes at least one used unit).
		make_virtual_machine(self.server, self.image, vcpus=1, memory_megabytes=512, disk_gigabytes=10)
		cluster = server_capacity.cluster_capacity()
		self.assertIsNotNone(cluster["share_units"])
		self.assertGreaterEqual(cluster["share_units"]["used"], 1)
		self.assertIn("cpu", cluster["stranded"])


class TestFakeServerAlwaysMeasured(IntegrationTestCase):
	"""A Fake host has no agent, but dev capacity math must always be *measured*:
	its totals are synthesized from the Fake size catalog, so no axis is ever
	uncatalogued (which would surface to Central as unmeasured→sentinel)."""

	def setUp(self) -> None:
		_clean_virtual_machines()
		frappe.db.set_single_value("Atlas Settings", "host_memory_reserve_megabytes", 0)
		# Build with the default DO fixture (its catalog is seeded), then flip the
		# row to a Fake host via db_set — the Fake size slug isn't a seeded Provider
		# Size, so it can't pass the Link check at insert.
		self.provider = make_provider("fake-capacity-provider")
		self.server = make_server(
			self.provider,
			"fake-capacity-server",
			ipv6_address="2001:db8:f::1",
			ipv6_prefix="2001:db8:f::/64",
			ipv6_virtual_machine_range="2001:db8:f::/124",
			status="Active",
		)
		self.server.db_set("provider_type", "Fake")
		self.server.db_set("size", "Fake/fake-4vcpu-8gb")

	def test_fake_totals_synthesized_from_size_catalog(self) -> None:
		from atlas.atlas.providers.fake import fake_host_totals

		totals = fake_host_totals("Fake/fake-4vcpu-8gb")
		self.assertEqual(totals["vcpus_total"], 4)
		self.assertEqual(totals["memory_megabytes_total"], 8192)
		self.assertGreater(totals["pool_disk_gigabytes_total"], 0, "disk is synthesized")

	def test_fake_host_every_axis_catalogued(self) -> None:
		# No agent ever stamped the row (all DB totals are 0), yet every axis must
		# report a real effective budget — never None (which is the sentinel path).
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["cpu"]["effective"], 4)
		self.assertEqual(result["memory"]["effective"], 8192)
		self.assertIsNotNone(result["disk"]["effective"])
		self.assertGreater(result["disk"]["effective"], 0)

	def test_fake_host_never_unmeasured_to_central(self) -> None:
		from atlas.atlas.api import provision

		for name in frappe.get_all("Server", filters={"status": "Active"}, pluck="name"):
			if name != self.server.name:
				frappe.db.set_value("Server", name, "status", "Draining")
		result = provision.capacity()
		self.assertFalse(result["unmeasured"], "Fake host is always measured")
		self.assertEqual(result["largest_vm"]["vcpus"], 4)
		self.assertEqual(result["largest_vm"]["memory_megabytes"], 8192)
