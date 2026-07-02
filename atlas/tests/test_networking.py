import ipaddress
import uuid

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.networking import (
	ATLAS_TUNNEL_SUPERNET,
	CPU_MODE_RELAXED,
	CPU_WEIGHT_MAX,
	CPU_WEIGHT_MIN,
	MAX_OPEN_FILES,
	MEMORY_HEADROOM_MIB,
	TUNNEL_PORT_BASE,
	UID_BASE,
	UID_SPAN,
	address_is_free_on_server,
	allocate_ipv6,
	carve_virtual_machine_range,
	cgroup_args,
	cpu_weight,
	derive_mac,
	derive_netns,
	derive_tap,
	derive_tunnel_interface,
	derive_uid,
	derive_veth_pair,
	resource_limit_args,
	tunnel_listen_port,
	tunnel_overlay_link,
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
	frappe.get_doc(
		{
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
		}
	).insert(ignore_permissions=True, set_name=name)
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

	# ----- address_is_free_on_server: the keep-address migration collision gate -----

	def test_address_free_when_unclaimed(self) -> None:
		server = _provider_and_server("free-server-1")
		for name in frappe.get_all("Virtual Machine", filters={"server": server}, pluck="name"):
			frappe.delete_doc("Virtual Machine", name, ignore_permissions=True, force=True)
		self.assertTrue(address_is_free_on_server(server, "2001:db8::2"))

	def test_address_not_free_when_live_vm_holds_it(self) -> None:
		"""The bug this guards: a keep-address migration carries a VM's /128 onto a
		target that already hosts a live VM on that same /128 — two VMs, one host, one
		address. address_is_free_on_server must report the collision (False)."""
		server = _provider_and_server("free-server-2")
		for name in frappe.get_all("Virtual Machine", filters={"server": server}, pluck="name"):
			frappe.delete_doc("Virtual Machine", name, ignore_permissions=True, force=True)
		_insert_vm(server, "2001:db8::2", status="Running")
		self.assertFalse(address_is_free_on_server(server, "2001:db8::2"))

	def test_address_free_when_only_terminated_holder(self) -> None:
		"""A Terminated VM has released the /128 — the address is free again."""
		server = _provider_and_server("free-server-3")
		for name in frappe.get_all("Virtual Machine", filters={"server": server}, pluck="name"):
			frappe.delete_doc("Virtual Machine", name, ignore_permissions=True, force=True)
		_insert_vm(server, "2001:db8::2", status="Terminated")
		self.assertTrue(address_is_free_on_server(server, "2001:db8::2"))

	def test_address_free_ignores_the_migrating_vm_itself(self) -> None:
		"""A resume/re-entry may have denormalized the migrating VM onto the target
		already; its own row must not count as a collision with itself."""
		server = _provider_and_server("free-server-4")
		for name in frappe.get_all("Virtual Machine", filters={"server": server}, pluck="name"):
			frappe.delete_doc("Virtual Machine", name, ignore_permissions=True, force=True)
		vm = _insert_vm(server, "2001:db8::2", status="Running")
		self.assertFalse(address_is_free_on_server(server, "2001:db8::2"))
		self.assertTrue(address_is_free_on_server(server, "2001:db8::2", ignore_vm=vm))

	def test_address_collision_normalizes_ipv6_forms(self) -> None:
		"""`::2` and its expanded form denote the same address — a stored VM in either
		form must still be seen as occupying the /128 (no string-equality escape)."""
		server = _provider_and_server("free-server-5")
		for name in frappe.get_all("Virtual Machine", filters={"server": server}, pluck="name"):
			frappe.delete_doc("Virtual Machine", name, ignore_permissions=True, force=True)
		_insert_vm(server, "2001:0db8:0000:0000:0000:0000:0000:0002", status="Running")
		self.assertFalse(address_is_free_on_server(server, "2001:db8::2"))

	def test_address_collision_is_per_server(self) -> None:
		"""A live VM on a DIFFERENT server does not block the address here — the
		conflict is per-host (one host, one route for the /128)."""
		server_a = _provider_and_server("free-server-6a")
		server_b = _provider_and_server("free-server-6b")
		for srv in (server_a, server_b):
			for name in frappe.get_all("Virtual Machine", filters={"server": srv}, pluck="name"):
				frappe.delete_doc("Virtual Machine", name, ignore_permissions=True, force=True)
		_insert_vm(server_b, "2001:db8::2", status="Running")
		self.assertTrue(address_is_free_on_server(server_a, "2001:db8::2"))

	# ----- jailer isolation derivations (pure functions of the UUID) -----

	def test_derive_uid_stable_and_in_range(self) -> None:
		name = str(uuid.uuid4())
		self.assertEqual(derive_uid(name), derive_uid(name))
		uid = derive_uid(name)
		self.assertGreaterEqual(uid, UID_BASE)
		self.assertLess(uid, UID_BASE + UID_SPAN)

	def test_derive_uid_spreads_across_uuids(self) -> None:
		# A degenerate derivation (e.g. a constant) would collapse the space.
		# 10k distinct UUIDs should produce a wide spread of distinct uids.
		uids = {derive_uid(str(uuid.uuid4())) for _ in range(10000)}
		# Birthday-paradox over a 60k space gives many collisions in 10k draws,
		# but we still expect the vast majority distinct — a broken derivation
		# would yield a tiny set.
		self.assertGreater(len(uids), 8000, len(uids))

	def test_derive_uid_matches_first_three_bytes(self) -> None:
		# Pin the formula so an accidental change to the byte window is caught.
		name = "d4f7c1a2-7e0a-4f1b-93cc-ad96b9b39b3e"
		self.assertEqual(derive_uid(name), UID_BASE + 0xD4F7C1 % UID_SPAN)

	def test_derive_netns_stable(self) -> None:
		name = str(uuid.uuid4())
		self.assertEqual(derive_netns(name), derive_netns(name))
		self.assertTrue(derive_netns(name).startswith("atlas-"))
		self.assertEqual(len(derive_netns(name)), len("atlas-") + 12)

	def test_derive_veth_pair_distinct_and_ifnamsiz_safe(self) -> None:
		for _ in range(20):
			name = str(uuid.uuid4())
			host_veth, ns_veth = derive_veth_pair(name)
			# Stable.
			self.assertEqual((host_veth, ns_veth), derive_veth_pair(name))
			# Distinct from each other and from the tap.
			self.assertNotEqual(host_veth, ns_veth)
			self.assertNotIn(derive_tap(name), (host_veth, ns_veth))
			# IFNAMSIZ-safe (<= 15 chars).
			self.assertLessEqual(len(host_veth), 15, host_veth)
			self.assertLessEqual(len(ns_veth), 15, ns_veth)
			self.assertTrue(host_veth.startswith("atlas-h"))
			self.assertTrue(ns_veth.startswith("atlas-n"))

	def test_derive_tunnel_interface_stable_and_ifnamsiz_safe(self) -> None:
		for _ in range(20):
			name = str(uuid.uuid4())
			interface = derive_tunnel_interface(name)
			self.assertEqual(interface, derive_tunnel_interface(name))
			self.assertTrue(interface.startswith("wg-"))
			# IFNAMSIZ-safe (<= 15 chars) and distinct from the VM tap/veth names.
			self.assertEqual(len(interface), len("wg-") + 11)
			self.assertLessEqual(len(interface), 15, interface)
			self.assertNotEqual(interface, derive_tap(name))
			self.assertNotIn(interface, derive_veth_pair(name))

	def test_tunnel_listen_port_indexes_from_base(self) -> None:
		self.assertEqual(tunnel_listen_port(0), TUNNEL_PORT_BASE)
		self.assertEqual(tunnel_listen_port(7), TUNNEL_PORT_BASE + 7)

	def test_tunnel_overlay_link_point_to_point(self) -> None:
		supernet = ipaddress.IPv6Network(ATLAS_TUNNEL_SUPERNET)
		seen = set()
		for slot in range(8):
			host_cidr, client_cidr = tunnel_overlay_link(slot)
			host = ipaddress.ip_interface(host_cidr)
			client = ipaddress.ip_interface(client_cidr)
			# /127 point-to-point: host is the lower address, client the upper,
			# and the two sit in the same /127.
			self.assertEqual(host.network.prefixlen, 127)
			self.assertEqual(host.network, client.network)
			self.assertLess(host.ip, client.ip)
			# Inside the supernet and unique across slots (per-host uniqueness).
			self.assertIn(host.ip, supernet)
			self.assertIn(client.ip, supernet)
			self.assertNotIn(host.ip, seen)
			self.assertNotIn(client.ip, seen)
			seen.update({host.ip, client.ip})
		# Slot 0 anchors at the supernet base, ::1 is the client end.
		self.assertEqual(
			tunnel_overlay_link(0),
			(f"{supernet.network_address}/127", f"{supernet.network_address + 1}/127"),
		)

	def test_cgroup_args_for_resource_triple(self) -> None:
		# 2 cores' bandwidth, 1024 MiB RAM, 8 GiB disk. cpu_max_cores=2 →
		# cpu.max quota 2*100000.
		args = cgroup_args(cpu_max_cores=2, memory_megabytes=1024, disk_gigabytes=8)
		expected_mem = (1024 + MEMORY_HEADROOM_MIB) * 1024 * 1024
		self.assertEqual(
			args,
			[
				"--cgroup",
				f"memory.max={expected_mem}",
				"--cgroup",
				"memory.swap.max=0",
				"--cgroup",
				"cpu.max=200000 100000",
			],
		)

	def test_cgroup_args_fractional_cpu_cap(self) -> None:
		# A sub-1 size: 1/16 of a core → cpu.max quota round(0.0625 * 100000) =
		# 6250, period 100000. This is the bandwidth cap; the guest still boots
		# with one vcpu_count thread (set from `vcpus`, not here).
		args = cgroup_args(cpu_max_cores=0.0625, memory_megabytes=256, disk_gigabytes=4)
		self.assertIn("cpu.max=6250 100000", args)
		# A 1/8 cap rounds to 12500.
		eighth = cgroup_args(cpu_max_cores=0.125, memory_megabytes=512, disk_gigabytes=6)
		self.assertIn("cpu.max=12500 100000", eighth)

	def test_cpu_weight_scales_and_clamps(self) -> None:
		# A full core is the cgroup default weight (100); shares scale linearly and
		# clamp into the kernel's [1, 10000] range so the smallest tier never
		# rounds to 0 and a huge VM never overflows.
		self.assertEqual(cpu_weight(1), 100)
		self.assertEqual(cpu_weight(0.0625), 6)  # 1/16 core
		self.assertEqual(cpu_weight(2), 200)
		self.assertEqual(cpu_weight(0.001), CPU_WEIGHT_MIN)  # rounds below 1 → clamped up
		self.assertEqual(cpu_weight(1000), CPU_WEIGHT_MAX)  # 100000 → clamped down

	def test_cgroup_args_relaxed_mode_weight_and_burst_ceiling(self) -> None:
		# Relaxed mode trades the hard cap for a cpu.weight floor (the guaranteed
		# share under contention) plus a loose cpu.max ceiling at vcpus whole
		# cores, so the VM bursts into idle host CPU. A 1/16 share, one vcpu:
		# weight 6, ceiling one core.
		args = cgroup_args(
			cpu_max_cores=0.0625,
			memory_megabytes=256,
			disk_gigabytes=4,
			cpu_mode=CPU_MODE_RELAXED,
			vcpus=1,
		)
		self.assertIn("cpu.weight=6", args)
		self.assertIn("cpu.max=100000 100000", args)
		# Memory caps are unchanged by the CPU model.
		expected_mem = (256 + MEMORY_HEADROOM_MIB) * 1024 * 1024
		self.assertIn(f"memory.max={expected_mem}", args)

	def test_cgroup_args_relaxed_ceiling_tracks_vcpus(self) -> None:
		# The burst ceiling is vcpus whole cores: a 4-vcpu relaxed VM may burst to
		# four cores, with its weight reflecting the (whole-core) share.
		args = cgroup_args(
			cpu_max_cores=4,
			memory_megabytes=1024,
			disk_gigabytes=8,
			cpu_mode=CPU_MODE_RELAXED,
			vcpus=4,
		)
		self.assertIn("cpu.weight=400", args)
		self.assertIn("cpu.max=400000 100000", args)

	def test_resource_limit_args_omits_fsize_for_lv_disk(self) -> None:
		# The VM disk is an LVM thin volume (a block device), not a regular file
		# the jailed process grows, so RLIMIT_FSIZE would not bound it — fsize is
		# omitted and only no-file remains. Pool-space accounting is the disk
		# guard. The disk arg is accepted but unused (signature parity with
		# cgroup_args).
		args = resource_limit_args(disk_gigabytes=8)
		self.assertEqual(
			args,
			[
				"--resource-limit",
				f"no-file={MAX_OPEN_FILES}",
			],
		)
		self.assertNotIn("--resource-limit\nfsize", "\n".join(args))
		self.assertFalse(any(a.startswith("fsize=") for a in args))
