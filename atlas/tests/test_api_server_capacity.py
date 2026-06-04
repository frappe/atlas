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
		# via db_set so we can pin the slug for the lookup test.
		self.server.db_set("size", "s-2vcpu-4gb-intel")
		self.image = make_image("capacity-test-image")

	def test_total_vcpus_from_size_slug(self) -> None:
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["total_vcpus"], 2)
		self.assertEqual(result["used_vcpus"], 0)
		self.assertEqual(result["virtual_machine_count"], 0)

	def test_effective_vcpus_equals_total_at_default_factor(self) -> None:
		# Default factor is 1 → effective budget is the physical total.
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["effective_vcpus"], 2)

	def test_effective_vcpus_scales_with_overprovision_factor(self) -> None:
		frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 16)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["total_vcpus"], 2, "physical total is unchanged")
		self.assertEqual(result["effective_vcpus"], 32, "budget is total × factor")

	def test_used_vcpus_sums_non_terminated_vms(self) -> None:
		make_virtual_machine(self.server, self.image, vcpus=1)
		make_virtual_machine(self.server, self.image, vcpus=2)
		terminated = make_virtual_machine(self.server, self.image, vcpus=4)
		terminated.db_set("status", "Terminated")

		result = server_capacity.capacity_for_server(self.server.name)
		self.assertEqual(result["used_vcpus"], 3)
		self.assertEqual(result["virtual_machine_count"], 2)

	def test_unknown_size_returns_none_total(self) -> None:
		self.server.db_set("size", "s-unknown-slug")
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertIsNone(result["total_vcpus"])
		self.assertIsNone(result["effective_vcpus"], "unknown size has unlimited budget")
		self.assertEqual(result["size"], "s-unknown-slug")

	def test_unknown_size_unlimited_even_with_factor(self) -> None:
		# An uncatalogued size stays unlimited regardless of the multiplier —
		# there is no physical total to scale.
		frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 16)
		self.server.db_set("size", "s-unknown-slug")
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertIsNone(result["effective_vcpus"])

	def test_missing_size_returns_none_total(self) -> None:
		self.server.db_set("size", None)
		result = server_capacity.capacity_for_server(self.server.name)
		self.assertIsNone(result["total_vcpus"])
		self.assertIsNone(result["effective_vcpus"])
		self.assertIsNone(result["size"])
