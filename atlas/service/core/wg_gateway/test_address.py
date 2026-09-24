from __future__ import annotations

import frappe
from frappe.tests import UnitTestCase

from atlas.service.core.wg_gateway.address import get_client_fdac, tenant_fdaa_prefix


class TestClientFdac(UnitTestCase):
	def test_the_tenant_and_client_fill_the_low_64_bits(self) -> None:
		self.assertEqual(get_client_fdac(1, 42, 7), "fdac:1:0:2a::7")

	def test_the_largest_tenant_and_client_fit(self) -> None:
		self.assertEqual(get_client_fdac(0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF), "fdac:ffff:ffff:ffff::ffff:ffff")

	def test_a_zero_tenant_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "Tenant ID"):
			get_client_fdac(1, 0, 7)

	def test_a_zero_client_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "Client ID"):
			get_client_fdac(1, 42, 0)

	def test_an_oversize_client_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "Client ID"):
			get_client_fdac(1, 42, 1 << 32)

	def test_a_non_integer_is_refused(self) -> None:
		with self.assertRaisesRegex(frappe.ValidationError, "Tenant ID"):
			get_client_fdac(1, "42", 7)


class TestTenantPrefix(UnitTestCase):
	def test_the_tenant_prefix_is_a_slash_64(self) -> None:
		self.assertEqual(tenant_fdaa_prefix(1, 42), "fdaa:1:0:2a::/64")
