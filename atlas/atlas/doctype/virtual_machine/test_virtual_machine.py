from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase


def _ensure_image() -> str:
	name = "vm-test-image-2"
	if frappe.db.exists("Virtual Machine Image", name):
		return name
	frappe.get_doc({
		"doctype": "Virtual Machine Image",
		"image_name": name,
		"kernel_url": "https://example.com/vmlinux",
		"kernel_filename": "vmlinux",
		"kernel_sha256": "a" * 64,
		"rootfs_url": "https://example.com/rootfs.squashfs",
		"rootfs_filename": "rootfs.ext4",
		"rootfs_sha256": "b" * 64,
		"default_disk_gigabytes": 4,
		"is_active": 1,
	}).insert(ignore_permissions=True)
	return name


def _ensure_server() -> str:
	provider_name = "vm-test-provider"
	if not frappe.db.exists("Server Provider", provider_name):
		frappe.get_doc({
			"doctype": "Server Provider",
			"provider_name": provider_name,
			"provider_type": "DigitalOcean",
			"api_token": "fake",
			"ssh_key_id": "fp",
			"ssh_private_key": "k",
			"default_region": "blr1",
			"default_size": "s",
			"default_image": "i",
			"is_active": 1,
		}).insert(ignore_permissions=True)
	server_name = "vm-test-server"
	if not frappe.db.exists("Server", server_name):
		frappe.get_doc({
			"doctype": "Server",
			"server_name": server_name,
			"provider": provider_name,
			"ipv4_address": "10.0.0.99",
			"ipv6_address": "2001:db8:1::1",
			"ipv6_prefix": "2001:db8:1::/64",
			"ipv6_virtual_machine_range": "2001:db8:1::/124",
			"status": "Active",
		}).insert(ignore_permissions=True)
	return server_name


def _new_vm(**overrides) -> "frappe.model.document.Document":
	server = _ensure_server()
	image = _ensure_image()
	defaults = {
		"doctype": "Virtual Machine",
		"description": "test vm",
		"server": server,
		"image": image,
		"vcpus": 1,
		"memory_megabytes": 512,
		"disk_gigabytes": 4,
		"ssh_public_key": "ssh-ed25519 AAAA",
	}
	defaults.update(overrides)
	return frappe.get_doc(defaults).insert(ignore_permissions=True)


class TestVirtualMachine(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_server()
		_ensure_image()
		# Clear VMs from prior tests so the /124 IPv6 range has capacity.
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def test_before_insert_sets_uuid_mac_tap_ipv6(self) -> None:
		vm = _new_vm()
		# Frappe-validated UUID: 36 chars with 4 dashes
		self.assertEqual(len(vm.name), 36)
		self.assertEqual(vm.name.count("-"), 4)
		self.assertTrue(vm.mac_address.startswith("06:00:"))
		self.assertTrue(vm.tap_device.startswith("atlas-"))
		self.assertEqual(len(vm.tap_device), 15)
		self.assertTrue(vm.ipv6_address.startswith("2001:db8:1::"))
		self.assertEqual(vm.status, "Pending")

	def test_immutable_fields_raise(self) -> None:
		vm = _new_vm()
		vm.vcpus = 4
		with self.assertRaises(frappe.ValidationError):
			vm.save(ignore_permissions=True)

	def test_provision_runs_when_image_present(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		probe = MagicMock(status="Success")
		main = MagicMock(name="task-x")
		main.name = "task-prov-1"

		with patch.object(module, "run_task_on_server", side_effect=[probe, main]):
			vm.provision()
		vm.reload()
		self.assertEqual(vm.status, "Running")
		self.assertIsNotNone(vm.last_started)

	def test_provision_raises_when_image_absent(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		original_status = vm.status
		# Probe raises ValidationError (Task Failure surfaces this).
		with patch.object(
			module,
			"run_task_on_server",
			side_effect=frappe.ValidationError("Image not present"),
		):
			with self.assertRaises(frappe.ValidationError):
				vm.provision()
		vm.reload()
		# Per spec/plan: VM stays in its current status; no Task is created
		# beyond the probe Task (which is itself the failure surface).
		self.assertEqual(vm.status, original_status)

	def test_provision_failure_marks_failed(self) -> None:
		from atlas.atlas.doctype.virtual_machine import virtual_machine as module

		vm = _new_vm()
		probe = MagicMock(status="Success")

		def side_effect(*args, **kwargs):
			if kwargs.get("script") == "probe-image-present.sh":
				return probe
			raise frappe.ValidationError("provision broke")

		with patch.object(module, "run_task_on_server", side_effect=side_effect):
			with self.assertRaises(frappe.ValidationError):
				vm.provision()
		vm.reload()
		self.assertEqual(vm.status, "Failed")
