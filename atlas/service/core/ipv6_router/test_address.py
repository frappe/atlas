from __future__ import annotations

import frappe
from frappe.tests import UnitTestCase

from atlas.service.core.ipv6_router.address import get_routed_ipv6


class TestRoutedIPv6(UnitTestCase):
	def test_the_tenant_and_vm_fill_the_low_48_bits(self) -> None:
		self.assertEqual(get_routed_ipv6("2001:db8:1:2:3::/80", "fdaa:1:0:2a::7"), "2001:db8:1:2:3:0:2a:7")

	def test_a_shorter_prefix_keeps_the_bits_above_the_host_part_zero(self) -> None:
		self.assertEqual(get_routed_ipv6("2001:db8:1::/48", "fdaa:1:0:2::3"), "2001:db8:1::2:3")

	def test_the_largest_tenant_and_vm_fit(self) -> None:
		self.assertEqual(
			get_routed_ipv6("2001:db8::/64", "fdaa:1:ffff:ffff::ffff"), "2001:db8::ffff:ffff:ffff"
		)

	def test_an_address_with_padding_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "padding"):
			get_routed_ipv6("2001:db8::/64", "fdaa:1:0:1::1:0")

	def test_a_block_smaller_than_80_bits_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "/80"):
			get_routed_ipv6("2001:db8::/84", "fdaa:1:0:1::1")

	def test_a_noncanonical_block_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "canonical IPv6 prefix"):
			get_routed_ipv6("2001:db8::1/64", "fdaa:1:0:1::1")

	def test_a_block_outside_global_unicast_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "within 2000::/3"):
			get_routed_ipv6("fd00::/64", "fdaa:1:0:1::1")

	def test_an_invalid_mesh_address_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "valid IPv6 address"):
			get_routed_ipv6("2001:db8::/64", "not-an-address")
