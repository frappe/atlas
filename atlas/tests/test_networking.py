import uuid

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.networking import (
	allocate_ipv6,
	carve_virtual_machine_range,
	derive_mac,
	derive_tap,
)


def _make_provider_and_server(server_name: str) -> None:
	provider_name = "test-prov-networking"
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
	if not frappe.db.exists("Server", server_name):
		frappe.get_doc({
			"doctype": "Server",
			"server_name": server_name,
			"provider": provider_name,
			"ipv4_address": "10.0.0.1",
			"ipv6_address": "2001:db8::1",
			"ipv6_prefix": "2001:db8::/64",
			"ipv6_virtual_machine_range": "2001:db8::/124",
			"status": "Active",
		}).insert(ignore_permissions=True)


def _insert_vm(server: str, address: str) -> None:
	# Insert a row directly to occupy an address. Skip the controller's
	# before_insert by using db_insert via frappe.get_doc with set_name.
	name = str(uuid.uuid4())
	frappe.get_doc({
		"doctype": "Virtual Machine",
		"__newname": name,
		"description": f"used-{address}",
		"server": server,
		"image": _ensure_image(),
		"vcpus": 1,
		"memory_megabytes": 512,
		"disk_gigabytes": 4,
		"ssh_public_key": "ssh-ed25519 AAAA",
		"status": "Pending",
	}).insert(ignore_permissions=True, set_name=name)
	# The controller's before_insert will have allocated its own IPv6; overwrite.
	frappe.db.set_value("Virtual Machine", name, "ipv6_address", address)


def _ensure_image() -> str:
	if frappe.db.exists("Virtual Machine Image", "vm-test-image"):
		return "vm-test-image"
	frappe.get_doc({
		"doctype": "Virtual Machine Image",
		"image_name": "vm-test-image",
		"kernel_url": "https://example.com/vmlinux",
		"kernel_filename": "vmlinux",
		"kernel_sha256": "a" * 64,
		"rootfs_url": "https://example.com/rootfs.squashfs",
		"rootfs_filename": "rootfs.ext4",
		"rootfs_sha256": "b" * 64,
		"default_disk_gigabytes": 4,
		"is_active": 1,
	}).insert(ignore_permissions=True)
	return "vm-test-image"


class TestNetworking(IntegrationTestCase):
	def test_carve_virtual_machine_range(self) -> None:
		self.assertEqual(
			carve_virtual_machine_range("2001:db8::/64"),
			"2001:db8::/124",
		)
		self.assertEqual(
			carve_virtual_machine_range("2a03:b0c0:abcd:1234::/64"),
			"2a03:b0c0:abcd:1234::/124",
		)

	def test_derive_mac_stable(self) -> None:
		name = str(uuid.uuid4())
		self.assertEqual(derive_mac(name), derive_mac(name))
		mac = derive_mac(name)
		self.assertTrue(mac.startswith("06:00:"))
		# 06:00 + 4 octets = 6 octets total = 17 chars including colons.
		self.assertEqual(len(mac), 17)

	def test_derive_tap_length_15(self) -> None:
		# Linux IFNAMSIZ is 16 bytes including the null terminator, so the
		# real max is 15 characters.
		for _ in range(20):
			tap = derive_tap(str(uuid.uuid4()))
			self.assertEqual(len(tap), 15, tap)
			self.assertTrue(tap.startswith("atlas-"))

	def test_allocate_ipv6_starts_at_2(self) -> None:
		server_name = "alloc-server-1"
		_make_provider_and_server(server_name)
		# Clean any existing VMs on this test server.
		for name in frappe.get_all("Virtual Machine", filters={"server": server_name}, pluck="name"):
			frappe.delete_doc("Virtual Machine", name, ignore_permissions=True, force=True)
		self.assertEqual(allocate_ipv6(server_name), "2001:db8::2")

	def test_allocate_ipv6_skips_used(self) -> None:
		server_name = "alloc-server-2"
		_make_provider_and_server(server_name)
		for name in frappe.get_all("Virtual Machine", filters={"server": server_name}, pluck="name"):
			frappe.delete_doc("Virtual Machine", name, ignore_permissions=True, force=True)
		_insert_vm(server_name, "2001:db8::2")
		_insert_vm(server_name, "2001:db8::3")
		self.assertEqual(allocate_ipv6(server_name), "2001:db8::4")

	def test_allocate_ipv6_raises_when_full(self) -> None:
		server_name = "alloc-server-3"
		_make_provider_and_server(server_name)
		for name in frappe.get_all("Virtual Machine", filters={"server": server_name}, pluck="name"):
			frappe.delete_doc("Virtual Machine", name, ignore_permissions=True, force=True)
		# /124 has 16 addresses (::0..::f); skip ::0 (subnet) and ::1 (host), so 14
		# usable. Fill them all.
		for octet in range(2, 16):
			_insert_vm(server_name, f"2001:db8::{octet:x}")
		with self.assertRaises(frappe.ValidationError):
			allocate_ipv6(server_name)
