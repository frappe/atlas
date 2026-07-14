"""Placement defaults for Virtual Machines.

A VM is created with only name / size / SSH key; the controller fills `server`
and `image` in before_insert (atlas/atlas/placement.py). These tests pin that the
fill happens, that `owner` is stamped from the acting user, and that the
no-capacity / ambiguous-image boundaries throw cleanly. No host — pure controller
logic (the after_insert provision enqueue is a no-op under frappe.in_test).
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import placement
from atlas.atlas.placement import (
	ConsolidationInProgressError,
	NoCapacityError,
	default_server,
	plan_consolidation,
)
from atlas.tests.fixtures import make_image, make_provider, make_server, make_virtual_machine

USER_EMAIL = "atlas-placement-user@example.com"


def _acting_user() -> str:
	"""A plain enabled User to act as — placement is operator-agnostic, so the
	role no longer matters; the test only needs a distinct session user to assert
	`owner` is stamped from it."""
	if frappe.db.exists("User", USER_EMAIL):
		return USER_EMAIL
	return (
		frappe.get_doc(
			{
				"doctype": "User",
				"email": USER_EMAIL,
				"first_name": "Place",
				"last_name": "Ment",
				"send_welcome_email": 0,
				"enabled": 1,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


class TestPlacement(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider("atlas-placement-provider")
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.db.set_single_value("Atlas Settings", "default_user_image", None)
		# No oversubscription unless a test opts in; keeps capacity assertions
		# independent of suite order.
		frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 1)
		# Isolate from the new capacity defaults: no memory floor and no arrival
		# reserve unless a test opts in (both default > 0 on a real site, but the
		# feasibility-boundary tests below stamp small totals and mean the raw budget).
		frappe.db.set_single_value("Atlas Settings", "host_memory_reserve_megabytes", 0)
		frappe.db.set_single_value("Atlas Settings", "placement_headroom_percent", 0)
		# Consolidation on with a generous cap, so a leaked 0 from the disabled test
		# can't turn a later consolidation test into a silent NoCapacityError.
		frappe.db.set_single_value("Atlas Settings", "placement_consolidation_enabled", 1)
		frappe.db.set_single_value("Atlas Settings", "max_consolidation_migrations", 3)
		# Wipe VMs left by other tests: servers are shared by title, so a stray
		# VM on the reused server would count against its vCPU budget and skew
		# the capacity-boundary tests below.
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)
		# Start from a clean slate: placement picks the first Active server and
		# throws on >1 active image, so neutralize any left by other suites /
		# fixtures so this test's own server+image are the only candidates.
		for name in frappe.get_all("Virtual Machine Image", filters={"is_active": 1}, pluck="name"):
			frappe.db.set_value("Virtual Machine Image", name, "is_active", 0)
		for name in frappe.get_all("Server", filters={"status": "Active"}, pluck="name"):
			frappe.db.set_value("Server", name, "status", "Draining")
		# Servers are reused by title across tests; a memory/disk total stamped by
		# one boundary test would leak into the CPU-only tests and refuse a VM on
		# an axis they don't mean to exercise. Clear every agent-reported total so
		# each test starts with only its own axes catalogued.
		for name in frappe.get_all("Server", pluck="name"):
			frappe.db.set_value(
				"Server",
				name,
				{
					"vcpus_total": 0,
					"memory_megabytes_total": 0,
					"pool_disk_gigabytes_total": 0,
					"placement_headroom_percent": 0,
				},
			)

	def _measured_server(self, title, host_octet, **totals):
		"""An Active server with a distinct /64, catalogued only on the axes in
		`totals` (the rest stay uncatalogued → unlimited). Distinct titles + IPv6
		ranges let a test stand up several placement candidates at once."""
		server = make_server(
			self.provider,
			title=title,
			size="DigitalOcean/s-4vcpu-8gb",
			ipv6_address=f"2001:db8:{host_octet}::1",
			ipv6_prefix=f"2001:db8:{host_octet}::/64",
			ipv6_virtual_machine_range=f"2001:db8:{host_octet}::/120",
		)
		image = make_image("atlas-placement-image")
		frappe.db.set_value("Virtual Machine Image", image.name, "is_active", 1)
		frappe.db.set_value("Server", server.name, "status", "Active")
		frappe.db.set_value(
			"Server",
			server.name,
			{"vcpus_total": 0, "memory_megabytes_total": 0, "pool_disk_gigabytes_total": 0, **totals},
		)
		return server

	def _new_machine(self, **overrides):
		"""Insert a VM the way the Central API does — no server, no image, and
		`ignore_permissions` (the real caller is operator orchestration authorized by
		the Central token, not desk RBAC). Frappe still stamps `owner` from the
		session user, so the owner-attribution assertion holds."""
		doc = {
			"doctype": "Virtual Machine",
			"title": "placement-vm",
			"size_preset": "Shared 1x",
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 4,
			"ssh_public_key": "ssh-ed25519 AAAA",
		}
		doc.update(overrides)
		return frappe.get_doc(doc).insert(ignore_permissions=True)

	def test_fills_server_and_image_and_owner(self) -> None:
		# setUp drained every Active server, so this is the only candidate.
		# Give it generous capacity so placement's vCPU check can't be the thing
		# under test here (capacity is exercised by test_no_active_server_throws).
		server = make_server(
			self.provider,
			title="atlas-placement-server",
			size="DigitalOcean/s-4vcpu-8gb",
			ipv6_address="2001:db8:1::1",
			ipv6_prefix="2001:db8:1::/64",
			ipv6_virtual_machine_range="2001:db8:1::/124",
		)
		image = make_image("atlas-placement-image")
		# make_image returns an existing row if present; setUp may have just
		# deactivated it, so re-assert active for the single-image happy path.
		frappe.db.set_value("Virtual Machine Image", image.name, "is_active", 1)
		frappe.db.set_value("Server", server.name, "status", "Active")

		user = _acting_user()
		frappe.set_user(user)
		vm = self._new_machine()

		self.assertEqual(vm.server, server.name, "server filled from the only active server")
		self.assertEqual(vm.image, image.name, "image filled from the single active image")
		self.assertEqual(vm.owner, user, "owner is stamped from the acting user")
		self.assertTrue(vm.ipv6_address, "ipv6 allocated against the filled server")

	def test_explicit_server_image_not_overridden(self) -> None:
		server = make_server(
			self.provider,
			title="atlas-placement-server",
			ipv6_address="2001:db8:1::1",
			ipv6_prefix="2001:db8:1::/64",
			ipv6_virtual_machine_range="2001:db8:1::/124",
		)
		image = make_image("atlas-placement-image")
		frappe.db.set_value("Server", server.name, "status", "Active")
		# Operator path: both supplied — placement is a no-op.
		vm = self._new_machine(server=server.name, image=image.name)
		self.assertEqual(vm.server, server.name)
		self.assertEqual(vm.image, image.name)

	def test_no_active_server_throws(self) -> None:
		image = make_image("atlas-placement-image")
		# setUp deactivates every image; re-assert active so default_image()
		# resolves and the throw genuinely comes from the no-server branch (not
		# from image resolution running first in apply_user_defaults).
		frappe.db.set_value("Virtual Machine Image", image.name, "is_active", 1)
		# A server exists but is not Active.
		make_server(self.provider, title="atlas-placement-server")
		frappe.set_user(_acting_user())
		# Typed NoCapacityError (a ValidationError subclass) so Central can tell
		# "region full" apart from a bad request — spec/16-central.md.
		with self.assertRaises(NoCapacityError):
			self._new_machine()

	def _full_4vcpu_server(self):
		"""An Active 4-vCPU server already running 4 vCPUs of VMs, plus a single
		active image. Shared setup for the overprovisioning boundary tests."""
		server = make_server(
			self.provider,
			title="atlas-placement-server",
			size="DigitalOcean/s-4vcpu-8gb",
			ipv6_address="2001:db8:1::1",
			ipv6_prefix="2001:db8:1::/64",
			ipv6_virtual_machine_range="2001:db8:1::/124",
		)
		image = make_image("atlas-placement-image")
		frappe.db.set_value("Virtual Machine Image", image.name, "is_active", 1)
		frappe.db.set_value("Server", server.name, "status", "Active")
		frappe.set_user(_acting_user())
		self._new_machine(vcpus=4, memory_megabytes=512, disk_gigabytes=4)
		return server

	def test_full_server_throws_at_default_factor(self) -> None:
		# Default factor 1: a 4-vCPU server with 4 vCPUs used has no room.
		self._full_4vcpu_server()
		with self.assertRaises(NoCapacityError):
			self._new_machine()

	def test_overprovision_factor_opens_room_on_full_server(self) -> None:
		# A 16x factor lifts the budget to 64 effective vCPUs, so the same
		# fully-booked server now accepts the VM.
		frappe.db.set_single_value("Atlas Settings", "overprovision_factor", 16)
		server = self._full_4vcpu_server()
		vm = self._new_machine()
		self.assertEqual(vm.server, server.name, "16x factor leaves room")

	def test_memory_full_refuses_even_with_cpu_and_disk_room(self) -> None:
		# A host with plenty of CPU (4 vCPU) but only 512 MB of RAM reported,
		# already spent by one VM, refuses a second VM on the memory axis alone.
		server = make_server(
			self.provider,
			title="atlas-placement-server",
			size="DigitalOcean/s-4vcpu-8gb",
			ipv6_address="2001:db8:1::1",
			ipv6_prefix="2001:db8:1::/64",
			ipv6_virtual_machine_range="2001:db8:1::/124",
		)
		image = make_image("atlas-placement-image")
		frappe.db.set_value("Virtual Machine Image", image.name, "is_active", 1)
		frappe.db.set_value("Server", server.name, "status", "Active")
		# Only RAM is catalogued+tight; CPU (slug) and disk (unset) have room.
		frappe.db.set_value("Server", server.name, "memory_megabytes_total", 512)
		frappe.set_user(_acting_user())
		self._new_machine(vcpus=1, memory_megabytes=512, disk_gigabytes=4)
		with self.assertRaises(NoCapacityError):
			self._new_machine(vcpus=1, memory_megabytes=512, disk_gigabytes=4)

	def test_disk_full_refuses_even_with_cpu_and_memory_room(self) -> None:
		# Same shape on the disk axis: pool disk total of 10 GB, spent by one VM,
		# refuses a second even though CPU and RAM have room.
		server = make_server(
			self.provider,
			title="atlas-placement-server",
			size="DigitalOcean/s-4vcpu-8gb",
			ipv6_address="2001:db8:1::1",
			ipv6_prefix="2001:db8:1::/64",
			ipv6_virtual_machine_range="2001:db8:1::/124",
		)
		image = make_image("atlas-placement-image")
		frappe.db.set_value("Virtual Machine Image", image.name, "is_active", 1)
		frappe.db.set_value("Server", server.name, "status", "Active")
		frappe.db.set_value("Server", server.name, "pool_disk_gigabytes_total", 10)
		frappe.set_user(_acting_user())
		self._new_machine(vcpus=1, memory_megabytes=512, disk_gigabytes=10)
		with self.assertRaises(NoCapacityError):
			self._new_machine(vcpus=1, memory_megabytes=512, disk_gigabytes=10)

	# --- relative-fill spread scorer (spec/28) -----------------------------

	def test_spread_alternates_across_equal_hosts(self) -> None:
		# Two equal measured hosts: consecutive VMs alternate — the emptier by
		# relative fill wins, so the second lands on the host the first didn't.
		host_a = self._measured_server("atlas-placement-a", 21, memory_megabytes_total=4096)
		self._measured_server("atlas-placement-b", 22, memory_megabytes_total=4096)
		frappe.set_user(_acting_user())
		first = self._new_machine(memory_megabytes=512)
		second = self._new_machine(memory_megabytes=512)
		self.assertNotEqual(first.server, second.server, "equal hosts alternate")
		self.assertEqual(first.server, host_a.name, "the creation-first host seats the first VM")

	def test_relative_fill_big_host_absorbs_more(self) -> None:
		# A host with twice the RAM takes twice the VMs — placement equalizes
		# *relative* fill, not absolute count. Only RAM is catalogued so it is the
		# sole binding axis; the big host is created first so ties resolve to it.
		big = self._measured_server("atlas-placement-big", 23, memory_megabytes_total=4096)
		small = self._measured_server("atlas-placement-small", 24, memory_megabytes_total=2048)
		frappe.set_user(_acting_user())
		for _ in range(3):
			self._new_machine(memory_megabytes=512)
		big_count = frappe.db.count("Virtual Machine", {"server": big.name})
		small_count = frappe.db.count("Virtual Machine", {"server": small.name})
		self.assertEqual((big_count, small_count), (2, 1), "2x RAM absorbs 2x the VMs")

	def test_fleet_reserve_blocks_new_vm_that_raw_budget_admits(self) -> None:
		# Raw effective (1024 MB) would admit a 768 MB VM, but a 50% arrival reserve
		# leaves only 512 MB for new placements → refused. With no reserve it fits.
		self._measured_server("atlas-placement-a", 21, memory_megabytes_total=1024)
		frappe.set_user(_acting_user())
		frappe.db.set_single_value("Atlas Settings", "placement_headroom_percent", 50)
		with self.assertRaises(NoCapacityError):
			self._new_machine(memory_megabytes=768)
		frappe.db.set_single_value("Atlas Settings", "placement_headroom_percent", 0)
		vm = self._new_machine(memory_megabytes=768)
		self.assertTrue(vm.server, "no reserve → the raw budget admits it")

	def test_per_server_override_beats_fleet_default(self) -> None:
		# Fleet default is 0 (pack full), but a per-server 90% reserve leaves only
		# ~102 MB for new placements on that host → a 512 MB VM is refused there.
		host = self._measured_server("atlas-placement-a", 21, memory_megabytes_total=1024)
		frappe.db.set_value("Server", host.name, "placement_headroom_percent", 90)
		frappe.set_user(_acting_user())
		with self.assertRaises(NoCapacityError):
			self._new_machine(memory_megabytes=512)
		# Drop the per-server override → it inherits the fleet 0 and admits.
		frappe.db.set_value("Server", host.name, "placement_headroom_percent", 0)
		vm = self._new_machine(memory_megabytes=512)
		self.assertEqual(vm.server, host.name)

	def test_measured_host_ranks_ahead_of_unmeasured(self) -> None:
		# A fully-measured host and an all-sentinel one both fit; the measured host
		# wins (fewer unmeasured axes) so placement prefers a host it can reason about.
		measured = self._measured_server(
			"atlas-placement-measured",
			21,
			vcpus_total=4,
			memory_megabytes_total=8192,
			pool_disk_gigabytes_total=160,
		)
		unmeasured = self._measured_server("atlas-placement-unmeasured", 22)
		# Unknown slug → no CPU fallback either, so every axis is uncatalogued.
		frappe.db.set_value("Server", unmeasured.name, "size", "s-unknown-slug")
		frappe.set_user(_acting_user())
		vm = self._new_machine(memory_megabytes=512)
		self.assertEqual(vm.server, measured.name, "measured host outranks the sentinel one")

	def test_tie_break_is_deterministic_by_creation(self) -> None:
		# Two identical empty measured hosts → the creation-first one wins, every time.
		first = self._measured_server("atlas-placement-a", 21, memory_megabytes_total=4096)
		self._measured_server("atlas-placement-b", 22, memory_megabytes_total=4096)
		frappe.set_user(_acting_user())
		vm = self._new_machine(memory_megabytes=512)
		self.assertEqual(vm.server, first.name)

	def test_ambiguous_image_throws(self) -> None:
		server = make_server(
			self.provider,
			title="atlas-placement-server",
			ipv6_address="2001:db8:1::1",
			ipv6_prefix="2001:db8:1::/64",
			ipv6_virtual_machine_range="2001:db8:1::/124",
		)
		frappe.db.set_value("Server", server.name, "status", "Active")
		make_image("atlas-placement-image-a")
		make_image("atlas-placement-image-b")
		frappe.set_user(_acting_user())
		with self.assertRaises(frappe.ValidationError):
			self._new_machine()

	def test_configured_default_image_resolves_ambiguity(self) -> None:
		server = make_server(
			self.provider,
			title="atlas-placement-server",
			ipv6_address="2001:db8:1::1",
			ipv6_prefix="2001:db8:1::/64",
			ipv6_virtual_machine_range="2001:db8:1::/124",
		)
		frappe.db.set_value("Server", server.name, "status", "Active")
		make_image("atlas-placement-image-a")
		image_b = make_image("atlas-placement-image-b")
		frappe.db.set_single_value("Atlas Settings", "default_user_image", image_b.name)
		frappe.set_user(_acting_user())
		vm = self._new_machine()
		self.assertEqual(vm.image, image_b.name, "configured default wins over ambiguity")

	# --- consolidation: free a host by migrating a few small VMs (spec/28 case 3) ---

	def _place_vm(self, server, memory, disk=4, status="Stopped", **overrides):
		"""A VM pinned to `server` at a given size, in a migratable state. Explicit
		server + image means apply_user_defaults is a no-op (no placement recursion)."""
		image = make_image("atlas-placement-image")
		vm = make_virtual_machine(
			server, image, memory_megabytes=memory, disk_gigabytes=disk, **overrides
		)
		vm.db_set("status", status)
		return vm

	def _make_migration(self, vm, source, target, status="Hydrating"):
		"""A non-terminal migration row of `vm` from `source` to `target`. Forces the
		address scheme so before_insert never has to probe the provider."""
		migration = frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"virtual_machine": vm.name,
				"source_server": source.name,
				"target_server": target.name,
				"status": status,
			}
		)
		migration.flags.keep_address_forced = True
		return migration.insert(ignore_permissions=True)

	def _fragmented_pair(self):
		"""Two equal 4096 MB (RAM-only) hosts, each 2560 MB full (a 2048 + a small 512),
		so 1536 MB is free on each — no host fits a 2048 MB arrival, but migrating one
		512 off a host frees a contiguous 2048 slot. Returns (host_a, host_b)."""
		host_a = self._measured_server("atlas-consolidate-a", 31, memory_megabytes_total=4096)
		host_b = self._measured_server("atlas-consolidate-b", 32, memory_megabytes_total=4096)
		self._place_vm(host_a, 2048)
		self._place_vm(host_a, 512)
		self._place_vm(host_b, 2048)
		self._place_vm(host_b, 512)
		return host_a, host_b

	def test_plan_consolidation_frees_a_host_with_one_small_move(self) -> None:
		host_a, host_b = self._fragmented_pair()
		plan = plan_consolidation({"cpu": 0.0625, "memory": 2048, "disk": 4})
		self.assertIsNotNone(plan, "a scattered fleet can be defragmented for the arrival")
		recipient, moves = plan
		self.assertEqual(len(moves), 1, "one small move is enough to free a host")
		vm_name, target = moves[0]
		self.assertNotEqual(recipient, target, "the evicted VM moves to a DIFFERENT host")
		self.assertEqual(
			frappe.db.get_value("Virtual Machine", vm_name, "memory_megabytes"),
			512,
			"it moves a SMALL VM, not the big one",
		)
		self.assertIn(recipient, (host_a.name, host_b.name))

	def test_default_server_signals_consolidation_and_enqueues(self) -> None:
		self._fragmented_pair()
		with patch("frappe.enqueue") as enqueue:
			with self.assertRaises(ConsolidationInProgressError):
				default_server(0.0625, 2048, 4)
			enqueue.assert_called_once()
			self.assertEqual(enqueue.call_args.args[0], "atlas.atlas.placement.consolidate")

	def test_consolidate_triggers_the_planned_migration(self) -> None:
		self._fragmented_pair()
		calls = []
		# consolidate() commits per move so one migration persists independently of the
		# next; stub the commit so that real commit can't break this IntegrationTestCase's
		# rollback isolation (it would leak the setUp fleet into later tests).
		with (
			patch.object(placement, "_start_migration", lambda vm, target: calls.append((vm, target))),
			patch("frappe.db.commit"),
		):
			result = placement.consolidate(0.0625, 2048, 4)
		self.assertEqual(len(calls), 1, "the planned move is actually triggered")
		self.assertEqual(result["triggered"][0]["virtual_machine"], calls[0][0])

	def test_consolidation_disabled_fails_loud(self) -> None:
		self._fragmented_pair()
		frappe.db.set_single_value("Atlas Settings", "placement_consolidation_enabled", 0)
		self.assertIsNone(plan_consolidation({"cpu": 0.0625, "memory": 2048, "disk": 4}))
		with patch("frappe.enqueue") as enqueue:
			with self.assertRaises(NoCapacityError):
				default_server(0.0625, 2048, 4)
			enqueue.assert_not_called()
		# Fail-loud must be the plain type, not the "retry, room is coming" signal.
		try:
			default_server(0.0625, 2048, 4)
		except ConsolidationInProgressError:
			self.fail("disabled consolidation must raise NoCapacityError, not the retry signal")
		except NoCapacityError:
			pass

	def test_single_host_cannot_consolidate(self) -> None:
		# One Active host with nowhere to move VMs to → no plan, plain NoCapacityError.
		host = self._measured_server("atlas-consolidate-solo", 33, memory_megabytes_total=2048)
		self._place_vm(host, 2048)
		self.assertIsNone(plan_consolidation({"cpu": 0.0625, "memory": 512, "disk": 4}))
		with self.assertRaises(NoCapacityError):
			default_server(0.0625, 512, 4)

	def test_in_flight_drain_waits_without_re_enqueuing(self) -> None:
		# A prior consolidation is already draining host A (its small VM migrates to B).
		# A fresh 2048 arrival must be told to WAIT (ConsolidationInProgressError) rather
		# than launch another migration — the idempotency that survives Central's retries.
		host_a = self._measured_server("atlas-consolidate-a", 31, memory_megabytes_total=4096)
		host_b = self._measured_server("atlas-consolidate-b", 32, memory_megabytes_total=4096)
		self._place_vm(host_a, 2048)
		small_a = self._place_vm(host_a, 512)
		self._place_vm(host_b, 1536)
		self._place_vm(host_b, 1024)
		self._make_migration(small_a, host_a, host_b)
		with patch("frappe.enqueue") as enqueue:
			with self.assertRaises(ConsolidationInProgressError):
				default_server(0.0625, 2048, 4)
			enqueue.assert_not_called()

	def test_consolidation_skips_vms_with_public_ipv4(self) -> None:
		# A VM with an attached Reserved IP must never be picked — moving it would
		# silently release inbound v4. Here the small 512 on host A is the ONLY move
		# that could free a host (host B's single 2560 VM has nowhere to go), so pinning
		# it with a public IPv4 must leave the fleet unconsolidatable.
		host_a = self._measured_server("atlas-consolidate-a", 31, memory_megabytes_total=4096)
		host_b = self._measured_server("atlas-consolidate-b", 32, memory_megabytes_total=4096)
		self._place_vm(host_a, 2048)
		pinned = self._place_vm(host_a, 512)  # free_a = 1536
		self._place_vm(host_b, 2560)  # free_b = 1536, single VM too big to re-home
		# Sanity: while it is movable, that 512 DOES free host A for a 2048 arrival.
		self.assertIsNotNone(plan_consolidation({"cpu": 0.0625, "memory": 2048, "disk": 4}))
		# Pin it with a public IPv4 → now nothing is safely movable → no plan.
		pinned.db_set("public_ipv4", "203.0.113.5")
		self.assertIsNone(plan_consolidation({"cpu": 0.0625, "memory": 2048, "disk": 4}))
