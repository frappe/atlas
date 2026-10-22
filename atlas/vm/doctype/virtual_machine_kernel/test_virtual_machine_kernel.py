from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.vm.core.vm_image_deletion import delete_unused_kernels

KERNEL_SHA256 = "b" * 64


def insert_kernel(sha256: str = KERNEL_SHA256) -> str:
	"""Insert one kernel record in object storage unless it exists, and return its name."""
	if not frappe.db.exists("Virtual Machine Kernel", sha256):
		frappe.get_doc(
			{
				"doctype": "Virtual Machine Kernel",
				"sha256": sha256,
				"architecture": "amd64",
				"size_mib": 8,
				"object_key": f"kernels/{sha256}",
			}
		).insert()
	return sha256


class TestVirtualMachineKernel(IntegrationTestCase):
	def test_only_an_unused_kernel_is_deleted_with_its_object(self) -> None:
		used_kernel = insert_kernel()
		unused_kernel = insert_kernel("c" * 64)
		frappe.get_doc(
			{
				"doctype": "Virtual Machine Image",
				"title": "kernel-user",
				"status": "Pending",
				"kernel": used_kernel,
			}
		).insert()
		client = Mock()

		with patch(
			"atlas.vm.doctype.virtual_machine_kernel.virtual_machine_kernel.frappe.get_single",
			return_value=Mock(get_object_storage_client=Mock(return_value=client)),
		):
			delete_unused_kernels()
			with self.assertRaises(frappe.ValidationError):
				frappe.delete_doc("Virtual Machine Kernel", used_kernel)

		self.assertTrue(frappe.db.exists("Virtual Machine Kernel", used_kernel))
		self.assertFalse(frappe.db.exists("Virtual Machine Kernel", unused_kernel))
		client.delete_object.assert_called_once_with(f"kernels/{unused_kernel}")
