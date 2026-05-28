import uuid

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.networking import (
	allocate_ipv6,
	carve_virtual_machine_range,
	derive_mac,
	derive_tap,
)
from atlas.tests.fixtures import make_image, make_provider, make_server


def _provider_and_server(title: str) -> str:
	"""Ensure a Server row with the given `title` exists and return its UUID `name`."""
	provider = make_provider("test-prov-networking")
	server = make_server(
		provider,
		title,
		ipv4_address="10.0.0.1",
		ipv6_address="2001:db8::1",
		ipv6_prefix="2001:db8::/64",
		ipv6_virtual_machine_range="2001:db8::/124",
		status="Active",
	)
	return server.name


def _ensure_image() -> str:
	return make_image("vm-test-image").name


def _insert_vm(server: str, address: str, status: str = "Pending") -> str:
	# Insert a row directly to occupy an address. Skip the controller's
	# before_insert by using db_insert via frappe.get_doc with set_name.
	name = str(uuid.uuid4())
	frappe.get_doc({
		"doctype": "Virtual Machine",
		"__newname": name,
		"title": f"used-{address}",
		"server": server,
		"image": _ensure_image(),
		"vcpus": 1,
		"memory_megabytes": 512,
		"disk_gigabytes": 4,
		"ssh_public_key": "ssh-ed25519 AAAA",
		"status": status,
	}).insert(ignore_permissions=True, set_name=name)
	# The controller's before_insert will have allocated its own IPv6; overwrite.
	frappe.db.set_value("Virtual Machine", name, "ipv6_address", address)
	return name


class TestNetworking(IntegrationTestCase):
	def test_carve_virtual_machine_range(self) -> None:
		# /124 around the host address, not the start of the /64. DO routes
		# the /124 that contains the droplet's own IPv6 — addresses outside
		# that /124 are unreachable from the internet.
		self.assertEqual(
			carve_virtual_machine_range(
				"2400:6180:100:d0:0:1:4ae1:d001",
				"2400:6180:100:d0::/64",
			),
			"2400:6180:100:d0:0:1:4ae1:d000/124",
		)
		self.assertEqual(
			carve_virtual_machine_range("2001:db8::1", "2001:db8::/64"),
			"2001:db8::/124",
		)
		with self.assertRaises(ValueError):
			carve_virtual_machine_range("2001:db8::1", "2a03::/64")

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
		server = _provider_and_server("alloc-server-1")
		# Clean any existing VMs on this test server.
		for name in frappe.get_all("Virtual Machine", filters={"server": server}, pluck="name"):
			frappe.delete_doc("Virtual Machine", name, ignore_permissions=True, force=True)
		self.assertEqual(allocate_ipv6(server), "2001:db8::2")

	def test_allocate_ipv6_skips_used(self) -> None:
		server = _provider_and_server("alloc-server-2")
		for name in frappe.get_all("Virtual Machine", filters={"server": server}, pluck="name"):
			frappe.delete_doc("Virtual Machine", name, ignore_permissions=True, force=True)
		_insert_vm(server, "2001:db8::2")
		_insert_vm(server, "2001:db8::3")
		self.assertEqual(allocate_ipv6(server), "2001:db8::4")

	def test_allocate_ipv6_raises_when_full(self) -> None:
		server = _provider_and_server("alloc-server-3")
		for name in frappe.get_all("Virtual Machine", filters={"server": server}, pluck="name"):
			frappe.delete_doc("Virtual Machine", name, ignore_permissions=True, force=True)
		# /124 has 16 addresses (::0..::f); skip ::0 (subnet) and ::1 (host), so 14
		# usable. Fill them all.
		for octet in range(2, 16):
			_insert_vm(server, f"2001:db8::{octet:x}")
		with self.assertRaises(frappe.ValidationError):
			allocate_ipv6(server)

	def test_allocate_ipv6_reuses_terminated_addresses(self) -> None:
		"""Terminated VMs release their address back into the pool."""
		server = _provider_and_server("alloc-server-4")
		for name in frappe.get_all("Virtual Machine", filters={"server": server}, pluck="name"):
			frappe.delete_doc("Virtual Machine", name, ignore_permissions=True, force=True)
		# ::2 held by a Terminated VM, ::3 still live. The next allocation should
		# pick ::2 (lowest unused, ignoring Terminated holders).
		_insert_vm(server, "2001:db8::2", status="Terminated")
		_insert_vm(server, "2001:db8::3", status="Running")
		self.assertEqual(allocate_ipv6(server), "2001:db8::2")
