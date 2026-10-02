from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from atlas.atlas.core import mesh_address
from atlas.atlas.core.mesh_address import get_region_mesh_address_prefix, get_virtual_machine_mesh_address


class TestMeshAddress(UnitTestCase):
	def setUp(self) -> None:
		settings = SimpleNamespace(region_id=1)
		self.get_settings_patch = patch.object(mesh_address.frappe, "get_single", return_value=settings)
		self.get_settings_patch.start()
		self.addCleanup(self.get_settings_patch.stop)

	def test_the_largest_vm_number_leaves_the_padding_zero(self) -> None:
		virtual_machine = frappe._dict(name="vm-7-65535", tenant_id=7)

		self.assertEqual(get_virtual_machine_mesh_address(virtual_machine), "fdaa:1:0:7::ffff")

	def test_the_vm_number_comes_after_the_tenant_in_the_name(self) -> None:
		virtual_machine = frappe._dict(name="vm-42-0007", tenant_id=42)

		self.assertEqual(get_virtual_machine_mesh_address(virtual_machine), "fdaa:1:0:2a::7")

	def test_a_vm_number_above_16_bits_is_refused(self) -> None:
		virtual_machine = frappe._dict(name="vm-7-65536", tenant_id=7)

		with self.assertRaises(frappe.ValidationError):
			get_virtual_machine_mesh_address(virtual_machine)

	def test_a_region_id_above_16_bits_is_refused(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			get_region_mesh_address_prefix(0x10000)

	def test_the_region_prefix_contains_the_mesh_and_region_hextets(self) -> None:
		self.assertEqual(get_region_mesh_address_prefix(1), "fdaa:1")
		self.assertEqual(get_region_mesh_address_prefix(2), "fdaa:2")
