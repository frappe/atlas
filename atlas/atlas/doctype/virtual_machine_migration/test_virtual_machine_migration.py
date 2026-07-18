"""Unit coverage for VM migration (spec/24): the pure parse, the phase machine,
the pre-flight throws, the immutability/retry contract, the flags.migrating gate,
and the lifecycle guard. Host facts (real NBD/dm-clone move) live in the e2e
use-case module; everything here runs in milliseconds with no host."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import migration as migration_module
from atlas.atlas.doctype.virtual_machine import virtual_machine as vm_module
from atlas.atlas.doctype.virtual_machine_migration.virtual_machine_migration import (
	active_migration_for,
)
from atlas.tests._mocks import fake_task
from atlas.tests.fixtures import make_image, make_provider, make_server, make_virtual_machine


def _source_server() -> str:
	provider = make_provider("mig-test-provider")
	return make_server(
		provider,
		"mig-source",
		ipv4_address="10.0.0.1",
		ipv6_address="2001:db8:9::1",
		ipv6_prefix="2001:db8:9::/64",
		ipv6_virtual_machine_range="2001:db8:9::/124",
		status="Active",
	).name


def _target_server(status: str = "Active") -> str:
	provider = make_provider("mig-test-provider")
	return make_server(
		provider,
		"mig-target",
		ipv4_address="10.0.0.2",
		ipv6_address="2001:db8:a::1",
		ipv6_prefix="2001:db8:a::/64",
		ipv6_virtual_machine_range="2001:db8:a::/124",
		status=status,
	).name


class TestMigrationPure(IntegrationTestCase):
	def test_nbd_port_is_stable_and_in_range(self) -> None:
		uuid = "5d0943c8-4e43-48ad-b652-3f181e22fc4d"
		port = migration_module.nbd_port(uuid)
		self.assertEqual(port, migration_module.nbd_port(uuid))  # stable
		self.assertTrue(10000 <= port < 15000)

	def test_vm_tunnel_helpers_are_stable_and_safe(self) -> None:
		from atlas.atlas import networking

		uuid = "5d0943c8-4e43-48ad-b652-3f181e22fc4d"
		device = networking.derive_vm_tunnel(uuid)
		self.assertEqual(device, networking.derive_vm_tunnel(uuid))  # stable
		self.assertTrue(device.startswith("mig6-"))
		self.assertLessEqual(len(device), 15)  # IFNAMSIZ-safe
		# Distinct from the other device families for the same UUID.
		self.assertNotEqual(device, networking.derive_tap(uuid))
		port = networking.derive_vm_tunnel_port(uuid)
		self.assertEqual(port, networking.derive_vm_tunnel_port(uuid))
		# Non-overlapping with the NBD-export window (10000-14999).
		self.assertGreaterEqual(port, 15000)
		table = networking.derive_vm_tunnel_table(uuid)
		self.assertEqual(table, networking.derive_vm_tunnel_table(uuid))
		self.assertGreaterEqual(table, 20000)  # clear of the reserved low table ids

	def test_hydration_parse(self) -> None:
		# <start> <len> clone <meta_used>/<meta_total> <region_size> <hydrated>/<total> ...
		parse = _parse_hydration()
		self.assertEqual(parse("0 8388608 clone 1/2048 32768 0/256 0 -"), 0)
		self.assertEqual(parse("0 8388608 clone 1/2048 32768 128/256 0 -"), 50)
		self.assertEqual(parse("0 8388608 clone 1/2048 32768 256/256 0 -"), 100)
		with self.assertRaises(ValueError):
			parse("garbage line")

	def test_nbd_base_slot_is_stable_and_in_range(self) -> None:
		uuid = "5d0943c8-4e43-48ad-b652-3f181e22fc4d"
		slot = migration_module.nbd_base_slot(uuid)
		self.assertEqual(slot, migration_module.nbd_base_slot(uuid))  # stable
		# A 4-slot block that fits in nbds_max=16: base in {0,4,8,12}, +3 <= 15.
		self.assertIn(slot, (0, 4, 8, 12))
		self.assertLessEqual(slot + migration_module.NBD_SLOTS_PER_MIGRATION - 1, 15)

	def test_nbd_base_slots_dont_overlap_across_vms(self) -> None:
		# Two UUIDs in different residue classes get disjoint 4-slot blocks — the
		# property that stops concurrent migrations from sharing an nbd device.
		a = "00000000-0000-0000-0000-000000000000"  # hex[4:8]=0000 -> slot 0
		b = "00000001-0000-0000-0000-000000000000"  # hex[4:8]=0001 -> slot 4
		sa, sb = migration_module.nbd_base_slot(a), migration_module.nbd_base_slot(b)
		self.assertNotEqual(sa, sb)
		block_a = set(range(sa, sa + migration_module.NBD_SLOTS_PER_MIGRATION))
		block_b = set(range(sb, sb + migration_module.NBD_SLOTS_PER_MIGRATION))
		self.assertEqual(block_a & block_b, set())

	def test_boot_then_hydrate_phase_order(self) -> None:
		"""Boot-then-hydrate (spec/24 §0): the guest must boot on the clone BEFORE
		hydration, and CollapseClone must follow it — so CutoverStarting precedes
		Hydrating precedes CollapseClone. Guards the reorder against a regression that
		would re-couple downtime to the copy."""
		order = migration_module.PHASE_ORDER
		self.assertLess(order.index("InjectingIdentity"), order.index("CutoverStarting"))
		self.assertLess(order.index("CutoverStarting"), order.index("Hydrating"))
		self.assertLess(order.index("Hydrating"), order.index("CollapseClone"))
		self.assertLess(order.index("CollapseClone"), order.index("Repointing"))
		# Every phase name has a handler and vice-versa.
		self.assertEqual(
			set(migration_module.PHASES),
			set(order) - {"Done"},
		)

	def test_clone_device_path_is_uuid_keyed(self) -> None:
		uuid = "5d0943c8-4e43-48ad-b652-3f181e22fc4d"
		self.assertEqual(
			migration_module.clone_device_path(uuid),
			f"/dev/mapper/atlas-vm-{uuid}-clone",
		)

	def test_bytes_to_gib_ceil_rounds_up(self) -> None:
		# The target base LV must be >= the source's byte size; a partial GiB rounds up.
		gib = 1024**3
		self.assertEqual(migration_module._bytes_to_gib_ceil(0), 0)
		self.assertEqual(migration_module._bytes_to_gib_ceil(1), 1)
		self.assertEqual(migration_module._bytes_to_gib_ceil(gib), 1)
		self.assertEqual(migration_module._bytes_to_gib_ceil(gib + 1), 2)
		self.assertEqual(migration_module._bytes_to_gib_ceil(5 * gib), 5)


class TestTargetDiskSizing(IntegrationTestCase):
	"""The target disk must be sized off the SOURCE's actual bytes, never the VM
	doc's declared disk_gigabytes — a grown disk (physically larger than the doc)
	would otherwise be truncated during hydration, killing the fs superblock
	(spec/24 §5, found on a real f1→f2 migration 2026-07-02)."""

	def setUp(self) -> None:
		self.source = _source_server()
		self.target = _target_server()
		self.image = make_image("mig-size-image").name
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def _doc_with(self, disk_gigabytes: int, data_disk_gigabytes: int = 0):
		vm = make_virtual_machine(
			self.source,
			self.image,
			disk_gigabytes=disk_gigabytes,
			data_disk_gigabytes=data_disk_gigabytes,
			status="Stopped",
		)
		return frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"virtual_machine": vm.name,
				"target_server": self.target,
			}
		).insert(ignore_permissions=True)

	def test_grown_source_disk_uses_source_bytes(self) -> None:
		# Doc says 20 GiB, but the source disk is physically 28 GiB → target must be 28.
		row = self._doc_with(disk_gigabytes=20)
		gib = 1024**3
		self.assertEqual(migration_module._target_disk_gb(row, "disk_gigabytes", 28 * gib), 28)

	def test_doc_size_wins_when_larger_than_source(self) -> None:
		# A never-grown disk: source bytes ~= doc size; the doc size is authoritative.
		row = self._doc_with(disk_gigabytes=40)
		gib = 1024**3
		self.assertEqual(migration_module._target_disk_gb(row, "disk_gigabytes", 20 * gib), 40)

	def test_absent_data_disk_is_zero(self) -> None:
		row = self._doc_with(disk_gigabytes=20, data_disk_gigabytes=0)
		self.assertEqual(migration_module._target_disk_gb(row, "data_disk_gigabytes", 0), 0)


def _parse_hydration():
	"""Load parse_hydration_percent from the on-disk script (its filename has dashes,
	so a normal import won't work — read + exec its module namespace)."""
	import os

	root = frappe.get_app_path("atlas", "..")
	path = os.path.join(root, "scripts", "migration-poll-hydration.py")
	# The script's sys.path shim + heavy imports (atlas._run) load fine on the
	# controller too; we only need the pure fn, so exec just that source.
	namespace: dict = {}
	src = open(path).read()
	# Strip the `sys.path.insert` + heavy imports block by exec-ing only the fn.
	start = src.index("def parse_hydration_percent")
	exec(compile(src[start:], path, "exec"), namespace)
	return namespace["parse_hydration_percent"]


class TestMigrationRow(IntegrationTestCase):
	def setUp(self) -> None:
		self.source = _source_server()
		self.target = _target_server()
		self.image = make_image("mig-test-image").name
		for name in frappe.get_all("Virtual Machine Migration", pluck="name"):
			frappe.delete_doc("Virtual Machine Migration", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def _vm(self, **overrides):
		return make_virtual_machine(self.source, self.image, **overrides)

	def _row(self, vm):
		return frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"virtual_machine": vm.name,
				"target_server": self.target,
			}
		).insert(ignore_permissions=True)

	def test_before_insert_denormalizes_source_and_address(self) -> None:
		vm = self._vm()
		row = self._row(vm)
		self.assertEqual(row.source_server, self.source)
		self.assertEqual(row.ipv6_address_old, vm.ipv6_address)
		self.assertEqual(row.status, "Pending")
		self.assertIsNotNone(row.started_at)

	def test_source_equals_target_raises(self) -> None:
		vm = self._vm()
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Virtual Machine Migration",
					"virtual_machine": vm.name,
					"source_server": self.source,
					"target_server": self.source,
				}
			).insert(ignore_permissions=True)

	def test_target_server_immutable_after_insert(self) -> None:
		vm = self._vm()
		row = self._row(vm)
		row.target_server = self.source
		with self.assertRaises(frappe.ValidationError):
			row.save(ignore_permissions=True)

	def test_active_migration_for(self) -> None:
		vm = self._vm()
		self.assertIsNone(active_migration_for(vm.name))
		row = self._row(vm)
		self.assertEqual(active_migration_for(vm.name), row.name)
		row.db_set("status", "Done")
		self.assertIsNone(active_migration_for(vm.name))

	def test_retry_only_from_failed_and_resumes_recorded_phase(self) -> None:
		vm = self._vm()
		row = self._row(vm)
		with self.assertRaises(frappe.ValidationError):
			row.retry()  # not Failed
		row.db_set({"status": "Failed", "error_at_status": "Hydrating", "error_message": "boom"})
		row.reload()
		row.retry()
		row.reload()
		self.assertEqual(row.status, "Hydrating")
		self.assertIsNone(row.error_message)

	def _change_address_row(self, vm):
		"""A migration row pinned to the change-address branch (a new /128 on the
		target), so a drive test walks the full phase order without a provider probe."""
		doc = frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"virtual_machine": vm.name,
				"target_server": self.target,
			}
		)
		doc.keep_address = 0
		doc.forward_address = 0
		doc.flags.keep_address_forced = True
		return doc.insert(ignore_permissions=True)

	def test_advance_migration_reports_more_work(self) -> None:
		"""advance_migration returns True while there's a further non-terminal phase to
		run immediately, and False once a phase HOLDS (Hydrating polling) or reaches a
		terminal phase — the signal start_migration uses to chain the next phase."""
		vm = self._vm(status="Stopped")
		row = self._change_address_row(vm)

		def _fake_run_task(*, script, variables, server, virtual_machine, timeout_seconds):
			if script == "migration-export-source":
				return fake_task(
					stdout='ATLAS_RESULT={"nbd_port": 10001, "nbd_pid": 4242, '
					'"root_size_bytes": 1, "data_size_bytes": 0}'
				)
			if script == "migration-poll-hydration":
				return fake_task(stdout='ATLAS_RESULT={"hydration_percent": 20}')  # holds
			return fake_task(stdout="ok")

		from atlas.atlas import proxy as proxy_module

		with (
			patch.object(migration_module, "run_task", side_effect=_fake_run_task),
			patch.object(proxy_module, "reconcile_proxies", return_value=[]),
		):
			# Pending → … → CutoverStarting each report "more work" (advance to a further
			# non-terminal phase), up to and including the boot that reaches Hydrating.
			row.reload()
			while row.status != "Hydrating":
				self.assertTrue(migration_module.advance_migration(row))
				row.reload()
			# Hydrating at 20% holds — no further phase to run now.
			self.assertFalse(migration_module.advance_migration(row))

	def test_start_migration_self_drives_until_terminal(self) -> None:
		"""start_migration re-enqueues itself after EVERY non-terminal step — a phase
		that advances AND a Hydrating poll that merely holds — so the migration drives
		itself to completion without depending on the reconcile_migrations cron. It stops
		re-enqueuing only once the row is terminal (Done/Failed)."""
		vm = self._vm(status="Stopped")
		row = self._change_address_row(vm)

		def _fake_run_task(*, script, variables, server, virtual_machine, timeout_seconds):
			if script == "migration-export-source":
				return fake_task(
					stdout='ATLAS_RESULT={"nbd_port": 10001, "nbd_pid": 4242, '
					'"root_size_bytes": 1, "data_size_bytes": 0}'
				)
			if script == "migration-poll-hydration":
				return fake_task(stdout='ATLAS_RESULT={"hydration_percent": 20}')  # holds
			return fake_task(stdout="ok")

		with (
			patch.object(migration_module, "run_task", side_effect=_fake_run_task),
			patch.object(migration_module.frappe, "enqueue") as enqueue,
		):
			# A phase that advances re-enqueues the next.
			migration_module.start_migration(row.name)
			self.assertEqual(enqueue.call_count, 1)
			self.assertEqual(enqueue.call_args.kwargs["name"], row.name)

			# Drive to Hydrating; every step (including the very last advance INTO
			# Hydrating) re-enqueues, since none of them is terminal.
			row.reload()
			while row.status != "Hydrating":
				enqueue.reset_mock()
				migration_module.start_migration(row.name)
				self.assertEqual(enqueue.call_count, 1)
				row.reload()

			# The holding Hydrating poll STILL re-enqueues — this is the self-drive loop
			# that carries the copy to 100% without the cron. (Was: asserted NO enqueue.)
			enqueue.reset_mock()
			migration_module.start_migration(row.name)  # Hydrating at 20% holds
			self.assertEqual(enqueue.call_count, 1)
			self.assertEqual(enqueue.call_args.kwargs["name"], row.name)
			self.assertEqual(row.reload().status, "Hydrating")


class TestAddressSchemeDerivation(IntegrationTestCase):
	"""The keep_address / forward_address derivation from the provider capability
	(spec/24 §2.8), independent of any live provider construction — the capability
	is mocked so the test pins the branching, not a vendor's real answer."""

	def setUp(self) -> None:
		self.source = _source_server()
		self.target = _target_server()
		self.image = make_image("mig-test-image").name
		for name in frappe.get_all("Virtual Machine Migration", pluck="name"):
			frappe.delete_doc("Virtual Machine Migration", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def _insert_row(self):
		vm = make_virtual_machine(self.source, self.image)
		return frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"virtual_machine": vm.name,
				"target_server": self.target,
			}
		).insert(ignore_permissions=True)

	def _with_forwardable(self, forwardable: bool):
		import atlas.atlas.providers as providers_module

		class _Stub:
			def vm_range_is_forwardable(self, _resource):
				return forwardable

		# _will_keep_address does a local `from atlas.atlas.providers import
		# for_provider_type`, so patch it at its source module.
		return patch.object(providers_module, "for_provider_type", return_value=_Stub())

	def test_forwardable_provider_keeps_address(self) -> None:
		with self._with_forwardable(True):
			row = self._insert_row()
		self.assertTrue(row.keep_address)

	def test_non_forwardable_provider_changes_address(self) -> None:
		with self._with_forwardable(False):
			row = self._insert_row()
		self.assertFalse(row.keep_address)
		self.assertFalse(row.forward_address)

	def test_forward_address_set_only_for_digitalocean(self) -> None:
		# Both test servers are DigitalOcean (the fixture default), so a kept
		# address here also sets forward_address (the proxy-NDP re-assert branch).
		with self._with_forwardable(True):
			row = self._insert_row()
		self.assertTrue(row.keep_address)
		self.assertTrue(row.forward_address)

	def test_scaleway_keeps_address_without_forward_flag(self) -> None:
		# A routed-prefix provider keeps the address but needs no NDP re-assert.
		frappe.db.set_value("Server", self.source, "provider_type", "Scaleway")
		frappe.db.set_value("Server", self.target, "provider_type", "Scaleway")
		with self._with_forwardable(True):
			row = self._insert_row()
		self.assertTrue(row.keep_address)
		self.assertFalse(row.forward_address)


class TestMigrationPreflight(IntegrationTestCase):
	def setUp(self) -> None:
		self.source = _source_server()
		self.target = _target_server()
		self.image = make_image("mig-test-image").name
		for name in frappe.get_all("Virtual Machine Migration", pluck="name"):
			frappe.delete_doc("Virtual Machine Migration", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def _vm(self, **overrides):
		overrides.setdefault("status", "Stopped")
		return make_virtual_machine(self.source, self.image, **overrides)

	def test_preflight_rejects_same_server(self) -> None:
		vm = self._vm()
		with self.assertRaisesRegex(frappe.ValidationError, "already on that server"):
			migration_module.preflight_checks(vm, self.source, False)

	def test_preflight_rejects_missing_target(self) -> None:
		vm = self._vm()
		with self.assertRaisesRegex(frappe.ValidationError, "does not exist"):
			migration_module.preflight_checks(vm, "no-such-server", False)

	def test_preflight_rejects_inactive_target(self) -> None:
		vm = self._vm()
		frappe.db.set_value("Server", self.target, "status", "Pending")
		with self.assertRaisesRegex(frappe.ValidationError, "not Active"):
			migration_module.preflight_checks(vm, self.target, False)

	def test_preflight_rejects_inflight(self) -> None:
		vm = self._vm()
		frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"virtual_machine": vm.name,
				"target_server": self.target,
			}
		).insert(ignore_permissions=True)
		with self.assertRaisesRegex(frappe.ValidationError, "in-flight migration"):
			migration_module.preflight_checks(vm, self.target, False)

	def test_preflight_keep_address_rejects_collision_on_target(self) -> None:
		"""The bug: a keep-address migration carries the VM's /128 onto a target that
		already hosts a live VM on that same /128. Pre-flight must reject it — two VMs
		cannot share a /128 on one host (the field outage: the target's own ::2 VM
		silently stole the migrated VM's traffic). Both fixture servers are
		DigitalOcean → vm_range_is_forwardable True → keep-address branch."""
		vm = self._vm()
		# Plant a live VM on the TARGET holding the address the migrating VM would keep.
		conflict = make_virtual_machine(self.target, self.image, status="Running")
		frappe.db.set_value("Virtual Machine", conflict.name, "ipv6_address", vm.ipv6_address)
		with self.assertRaisesRegex(frappe.ValidationError, "already hosts a live VM"):
			migration_module.preflight_checks(vm, self.target, False)

	def test_preflight_keep_address_allows_when_target_free(self) -> None:
		"""The kept /128 is free on the target (only a Terminated holder) → no raise."""
		vm = self._vm()
		freed = make_virtual_machine(self.target, self.image, status="Terminated")
		frappe.db.set_value("Virtual Machine", freed.name, "ipv6_address", vm.ipv6_address)
		# Must not raise.
		migration_module.preflight_checks(vm, self.target, False)


class TestMigrationGateAndGuard(IntegrationTestCase):
	def setUp(self) -> None:
		self.source = _source_server()
		self.target = _target_server()
		self.image = make_image("mig-test-image").name
		for name in frappe.get_all("Virtual Machine Migration", pluck="name"):
			frappe.delete_doc("Virtual Machine Migration", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def _vm(self, **overrides):
		return make_virtual_machine(self.source, self.image, **overrides)

	def test_server_change_blocked_without_flag(self) -> None:
		vm = self._vm()
		vm.server = self.target
		with self.assertRaisesRegex(frappe.ValidationError, "immutable"):
			vm.save(ignore_permissions=True)

	def test_server_change_allowed_with_migrating_flag(self) -> None:
		vm = self._vm()
		vm.flags.migrating = True
		vm.server = self.target
		vm.save(ignore_permissions=True)  # must not raise
		vm.reload()
		self.assertEqual(vm.server, self.target)

	def test_lifecycle_guard_blocks_start_during_migration(self) -> None:
		vm = self._vm(status="Stopped")
		frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"virtual_machine": vm.name,
				"target_server": self.target,
			}
		).insert(ignore_permissions=True)
		with self.assertRaisesRegex(frappe.ValidationError, "in-flight migration"):
			vm.start()


class TestMigrationPhaseMachine(IntegrationTestCase):
	"""Drive the phase machine with run_task mocked — proves the phase ORDER,
	idempotency, and the state transitions without any host."""

	def setUp(self) -> None:
		self.source = _source_server()
		self.target = _target_server()
		self.image = make_image("mig-test-image").name
		for name in frappe.get_all("Virtual Machine Migration", pluck="name"):
			frappe.delete_doc("Virtual Machine Migration", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def _vm(self, **overrides):
		return make_virtual_machine(self.source, self.image, status="Stopped", **overrides)

	def _row(self, vm, keep_address: int | None = None):
		doc = frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"virtual_machine": vm.name,
				"target_server": self.target,
			}
		)
		if keep_address is not None:
			# Force the address scheme instead of letting the provider capability
			# decide, so a phase test pins exactly the branch it means to exercise.
			doc.keep_address = keep_address
			doc.forward_address = 0
			doc.flags.keep_address_forced = True
		return doc.insert(ignore_permissions=True)

	def test_phases_advance_in_order(self) -> None:
		# Pin the change-address path (a new /128 on the target); the keep-address
		# path is covered by test_keep_address_phases_keep_the_128.
		vm = self._vm()
		row = self._row(vm, keep_address=0)

		# Fake host results per script. run_task returns a Task-like with .stdout
		# carrying the ATLAS_RESULT the phase parses.
		def _fake_run_task(*, script, variables, server, virtual_machine, timeout_seconds):
			if script == "migration-export-source":
				return fake_task(
					stdout='ATLAS_RESULT={"nbd_port": 10001, "nbd_pid": 4242, '
					'"root_size_bytes": 1, "data_size_bytes": 0}'
				)
			if script == "migration-poll-hydration":
				return fake_task(stdout='ATLAS_RESULT={"hydration_percent": 100}')
			return fake_task(stdout="ok")

		# Boot-then-hydrate order (spec/24 §0): CutoverStarting (boot on the clone)
		# now precedes Hydrating (copy while serving), and CollapseClone follows it.
		expected = [
			"ExportingSnapshot",
			"TargetPreparing",
			"InjectingIdentity",
			"CutoverStarting",
			"Hydrating",
			"CollapseClone",
			"Repointing",
			"Cleanup",
			"Done",
		]
		from atlas.atlas import proxy as proxy_module

		with (
			patch.object(migration_module, "run_task", side_effect=_fake_run_task),
			patch.object(proxy_module, "reconcile_proxies", return_value=[]),
		):
			for want in expected:
				row.reload()
				migration_module.advance_migration(row)
				row.reload()
				self.assertEqual(row.status, want, f"after advancing expected {want}")

		vm.reload()
		self.assertEqual(vm.server, self.target)
		self.assertEqual(vm.status, "Running")
		self.assertTrue(str(vm.ipv6_address).startswith("2001:db8:a::"))
		self.assertIsNotNone(row.completed_at)

	def test_keep_address_phases_keep_the_128(self) -> None:
		"""The keep-address path: same phase ORDER, but the VM keeps its /128, no
		Subdomain re-point happens, and the VM is recorded as forwarded from the
		source (spec/24 §2.9)."""
		vm = self._vm()
		original_ipv6 = vm.ipv6_address
		row = self._row(vm, keep_address=1)

		scripts_seen: list[str] = []

		def _fake_run_task(*, script, variables, server, virtual_machine, timeout_seconds):
			scripts_seen.append(script)
			if script == "migration-export-source":
				return fake_task(
					stdout='ATLAS_RESULT={"nbd_port": 10001, "nbd_pid": 4242, '
					'"root_size_bytes": 1, "data_size_bytes": 0}'
				)
			if script == "migration-poll-hydration":
				return fake_task(stdout='ATLAS_RESULT={"hydration_percent": 100}')
			return fake_task(stdout="ok")

		from atlas.atlas import proxy as proxy_module

		with (
			patch.object(migration_module, "run_task", side_effect=_fake_run_task),
			patch.object(proxy_module, "reconcile_proxies", return_value=[]) as reconcile,
		):
			for _ in range(9):
				row.reload()
				migration_module.advance_migration(row)
			row.reload()

		self.assertEqual(row.status, "Done")
		vm.reload()
		# server flipped, address UNCHANGED (the source forwards the same /128).
		self.assertEqual(vm.server, self.target)
		self.assertEqual(vm.status, "Running")
		self.assertEqual(vm.ipv6_address, original_ipv6)
		# The forward is recorded on the VM (drives the dashboard + collapse action).
		self.assertEqual(vm.traffic_forwarded_from, self.source)
		self.assertIsNotNone(vm.traffic_forwarded_since)
		# The tunnel scripts ran; the proxy was NOT reconciled (nothing moved).
		self.assertIn("migration-forward-up", scripts_seen)
		self.assertIn("migration-target-receive", scripts_seen)
		self.assertIn("migration-source-forward", scripts_seen)
		reconcile.assert_not_called()
		# Row observability: forwarding, tunnel device recorded.
		self.assertEqual(row.tunnel_status, "Forwarding")
		self.assertTrue(row.forward_active)
		self.assertTrue(row.tunnel_device.startswith("mig6-"))

	def test_cutover_reasserts_proxy_ndp_even_when_not_forward_address(self) -> None:
		"""Regression: the source must re-assert proxy-NDP at cutover on EVERY provider,
		not just DigitalOcean. With forward_address=0 (the routed-prefix / Scaleway case)
		the old code passed REASSERT_PROXY_NDP=0, so the source stopped answering NDP and
		the switch black-holed ALL public inbound to the kept /128 (egress still worked —
		the field symptom: 'can't ping from the controller'). Assert the source-forward
		call carries REASSERT_PROXY_NDP=1 regardless of forward_address."""
		vm = self._vm()
		row = self._row(vm, keep_address=1)
		# Pin the routed-prefix case: keep-address but NOT a proxy-NDP-primary provider.
		row.db_set("forward_address", 0)

		source_forward_vars: dict = {}

		def _fake_run_task(*, script, variables, server, virtual_machine, timeout_seconds):
			if script == "migration-source-forward":
				source_forward_vars.update(variables)
			if script == "migration-export-source":
				return fake_task(
					stdout='ATLAS_RESULT={"nbd_port": 10001, "nbd_pid": 4242, '
					'"root_size_bytes": 1, "data_size_bytes": 0}'
				)
			if script == "migration-poll-hydration":
				return fake_task(stdout='ATLAS_RESULT={"hydration_percent": 100}')
			return fake_task(stdout="ok")

		from atlas.atlas import proxy as proxy_module

		with (
			patch.object(migration_module, "run_task", side_effect=_fake_run_task),
			patch.object(proxy_module, "reconcile_proxies", return_value=[]),
		):
			for _ in range(9):
				row.reload()
				migration_module.advance_migration(row)

		self.assertEqual(source_forward_vars.get("REASSERT_PROXY_NDP"), "1")

	def test_hydrating_rebuilds_clone_when_source_client_dies(self) -> None:
		"""A dead source nbd client (poll reports source_healthy=false) is self-healed:
		Hydrating re-runs the clone-prepare step to rebuild the stack, resets the
		tracked percent to 0 (the rebuilt clone hydrates afresh), does NOT advance, and
		does NOT count the drop toward the stall guard (it's recoverable, not stuck)."""
		vm = self._vm()
		row = self._row(vm, keep_address=0)

		poll_healthy = {"value": False}  # first poll: client dead
		scripts_seen: list[str] = []

		def _fake_run_task(*, script, variables, server, virtual_machine, timeout_seconds):
			scripts_seen.append(script)
			if script == "migration-export-source":
				return fake_task(
					stdout='ATLAS_RESULT={"nbd_port": 10001, "nbd_pid": 4242, '
					'"root_size_bytes": 1, "data_size_bytes": 0}'
				)
			if script == "migration-poll-hydration":
				healthy = "true" if poll_healthy["value"] else "false"
				pct = 40 if poll_healthy["value"] else 0
				return fake_task(
					stdout=f'ATLAS_RESULT={{"hydration_percent": {pct}, "source_healthy": {healthy}}}'
				)
			return fake_task(stdout="ok")

		from atlas.atlas import proxy as proxy_module

		with (
			patch.object(migration_module, "run_task", side_effect=_fake_run_task),
			patch.object(proxy_module, "reconcile_proxies", return_value=[]),
		):
			# Drive to Hydrating (boot-then-hydrate: Pending → ExportingSnapshot →
			# TargetPreparing → InjectingIdentity → CutoverStarting → Hydrating).
			row.reload()
			while row.status != "Hydrating":
				migration_module.advance_migration(row)
				row.reload()
			self.assertEqual(row.status, "Hydrating")
			# Pretend hydration got partway before the client died.
			row.db_set({"hydration_percent": 58, "hydration_stall_ticks": 3})

			# The unhealthy poll: rebuild fires, no advance, progress + stall reset.
			scripts_seen.clear()
			row.reload()
			self.assertFalse(migration_module.advance_migration(row))
			row.reload()
			self.assertEqual(row.status, "Hydrating")  # held, not failed
			self.assertIn("migration-clone-target", scripts_seen)  # prepare re-ran
			self.assertEqual(row.hydration_percent, 0)  # reset for the fresh clone
			self.assertEqual(row.hydration_stall_ticks, 0)  # drop is not a stall

			# Client recovers: normal polling resumes and advances at 100%.
			poll_healthy["value"] = True
			row.reload()
			migration_module.advance_migration(row)
			row.reload()
			self.assertEqual(row.hydration_percent, 40)


class TestLocalBaseImageShip(IntegrationTestCase):
	"""spec/24 §5.1: a VM on a LOCAL (snapshot-promoted, un-syncable) base image
	must ship that base to the target during TargetPreparing. Drives the phase
	machine with run_task mocked, proving the base-ship sub-phase runs the right
	host scripts, re-enters TargetPreparing until the base is 100% hydrated, records
	progress, and only then proceeds to the disk clone."""

	def setUp(self) -> None:
		self.source = _source_server()
		self.target = _target_server()
		# A LOCAL image: no URLs, so is_local is True and sync-image can't place it.
		self.image = make_image(
			"mig-local-image",
			kernel_url="",
			rootfs_url="",
			kernel_sha256="",
			rootfs_sha256="",
		).name
		for name in frappe.get_all("Virtual Machine Migration", pluck="name"):
			frappe.delete_doc("Virtual Machine Migration", name, force=1, ignore_permissions=True)
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def _row(self):
		vm = make_virtual_machine(self.source, self.image, status="Stopped")
		doc = frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"virtual_machine": vm.name,
				"target_server": self.target,
			}
		)
		doc.keep_address = 0
		doc.forward_address = 0
		doc.flags.keep_address_forced = True
		return doc.insert(ignore_permissions=True), vm

	def test_image_is_local_detects_missing_rootfs_url(self) -> None:
		self.assertTrue(migration_module._image_is_local(self.image))
		syncable = make_image("mig-syncable-image").name
		self.assertFalse(migration_module._image_is_local(syncable))

	def test_local_base_ships_before_disk_clone(self) -> None:
		row, _vm = self._row()

		scripts_seen: list[str] = []
		# The base clone hydrates over TWO ticks (40% then 100%) to prove the phase
		# re-enters; the VM DISK poll always reports 100 so the later Hydrating phase
		# doesn't add ticks we have to count. We tell them apart by clone_device.
		base_polls = iter([40, 100])

		def _fake_run_task(*, script, variables, server, virtual_machine, timeout_seconds):
			scripts_seen.append(script)
			if script == "migration-export-source":
				return fake_task(
					stdout='ATLAS_RESULT={"nbd_port": 10001, "nbd_pid": 4242, '
					'"root_size_bytes": 1, "data_size_bytes": 0}'
				)
			if script == "migration-export-base":
				return fake_task(
					stdout='ATLAS_RESULT={"nbd_port": 10003, "nbd_pid": 5252, '
					'"base_size_bytes": 2147483649, "meta_port": 10004, '
					'"meta_pid": 5253, "meta_size_bytes": 4096}'
				)
			if script == "migration-poll-hydration":
				# Base clone device vs the VM disk: the base ship passes clone_device.
				if variables.get("CLONE_DEVICE"):
					return fake_task(stdout=f'ATLAS_RESULT={{"hydration_percent": {next(base_polls)}}}')
				return fake_task(stdout='ATLAS_RESULT={"hydration_percent": 100}')
			return fake_task(stdout="ok")

		from atlas.atlas import proxy as proxy_module

		with (
			patch.object(migration_module, "run_task", side_effect=_fake_run_task),
			patch.object(proxy_module, "reconcile_proxies", return_value=[]),
		):
			# Advance to TargetPreparing.
			for _ in range(2):
				row.reload()
				migration_module.advance_migration(row)
			row.reload()
			self.assertEqual(row.status, "TargetPreparing")

			# First TargetPreparing tick: base ship starts, hydrates to 40% — the phase
			# does NOT advance (still shipping), records percent, and has NOT yet run
			# the disk clone.
			migration_module.advance_migration(row)
			row.reload()
			self.assertEqual(row.status, "TargetPreparing")
			self.assertEqual(row.base_ship_state, "Shipping")
			self.assertEqual(row.base_ship_percent, 40)
			self.assertEqual(row.progress_percent, 40)
			self.assertIn("migration-export-base", scripts_seen)
			self.assertIn("migration-receive-base", scripts_seen)
			self.assertNotIn("migration-clone-target", scripts_seen)

			# Second tick: base hits 100%, finalizes, and the SAME tick proceeds to the
			# disk clone and advances the phase.
			migration_module.advance_migration(row)
			row.reload()
			self.assertEqual(row.base_ship_state, "Done")
			self.assertIn("migration-clone-target", scripts_seen)
			self.assertEqual(row.status, "InjectingIdentity")

		# The disk clone came only AFTER the base ship (both prepare + finalize ran).
		self.assertGreaterEqual(scripts_seen.count("migration-receive-base"), 2)
		self.assertLess(
			scripts_seen.index("migration-receive-base"),
			scripts_seen.index("migration-clone-target"),
		)

	def test_base_ship_rebuilds_when_source_client_dies(self) -> None:
		"""A dead source nbd client mid base-image ship (base poll reports
		source_healthy=false) is self-healed exactly like the VM-disk Hydrating phase:
		the base ship does NOT advance, resets base_ship_percent to 0, and re-enters —
		the next tick's receive-base prepare rebuilds the wedged clone. Without this the
		base clone spins on dead reads forever (par-1-2 2026-07-07: hydration failed /
		nbd I/O-error log storm for two days)."""
		row, _vm = self._row()

		poll_healthy = {"value": False}  # first base poll: source client dead
		receive_base_calls: list[str] = []

		def _fake_run_task(*, script, variables, server, virtual_machine, timeout_seconds):
			if script == "migration-export-source":
				return fake_task(
					stdout='ATLAS_RESULT={"nbd_port": 10001, "nbd_pid": 4242, '
					'"root_size_bytes": 1, "data_size_bytes": 0}'
				)
			if script == "migration-export-base":
				return fake_task(
					stdout='ATLAS_RESULT={"nbd_port": 10003, "nbd_pid": 5252, '
					'"base_size_bytes": 1, "meta_port": 10004, "meta_pid": 5253, '
					'"meta_size_bytes": 4096}'
				)
			if script == "migration-receive-base":
				receive_base_calls.append(variables.get("PHASE", ""))
				return fake_task(stdout="ok")
			if script == "migration-poll-hydration":
				# Base clone poll carries CLONE_DEVICE; the VM disk poll doesn't.
				if variables.get("CLONE_DEVICE"):
					healthy = "true" if poll_healthy["value"] else "false"
					pct = 100 if poll_healthy["value"] else 0
					return fake_task(
						stdout=f'ATLAS_RESULT={{"hydration_percent": {pct}, "source_healthy": {healthy}}}'
					)
				return fake_task(stdout='ATLAS_RESULT={"hydration_percent": 100}')
			return fake_task(stdout="ok")

		from atlas.atlas import proxy as proxy_module

		with (
			patch.object(migration_module, "run_task", side_effect=_fake_run_task),
			patch.object(proxy_module, "reconcile_proxies", return_value=[]),
		):
			# Advance to TargetPreparing.
			for _ in range(2):
				row.reload()
				migration_module.advance_migration(row)
			row.reload()
			self.assertEqual(row.status, "TargetPreparing")

			# Unhealthy base poll: the ship holds (no advance), resets percent, and did
			# NOT finalize — the phase stays in TargetPreparing to rebuild next tick.
			receive_base_calls.clear()
			migration_module.advance_migration(row)
			row.reload()
			self.assertEqual(row.status, "TargetPreparing")  # held, not advanced
			self.assertEqual(row.base_ship_state, "Shipping")  # not Done
			self.assertEqual(row.base_ship_percent, 0)  # reset for the rebuilt clone
			self.assertIn("prepare", receive_base_calls)  # prepare ran (self-repair)
			self.assertNotIn("finalize", receive_base_calls)  # never collapsed a dead clone

			# Source recovers: the base ship reaches 100%, finalizes, and proceeds.
			poll_healthy["value"] = True
			row.reload()
			migration_module.advance_migration(row)
			row.reload()
			self.assertEqual(row.base_ship_state, "Done")

	def test_progress_detail_is_stamped_before_each_phase(self) -> None:
		# spec/24: progress must be visible at all points — advance_migration stamps a
		# live progress_detail line naming the host BEFORE the (possibly slow) phase
		# task runs, and the disk hydration writes a finer line + percent per tick.
		row, _vm = self._row()

		def _fake_run_task(*, script, variables, server, virtual_machine, timeout_seconds):
			if script == "migration-export-source":
				return fake_task(
					stdout='ATLAS_RESULT={"nbd_port": 10001, "nbd_pid": 4242, '
					'"root_size_bytes": 1, "data_size_bytes": 0}'
				)
			if script == "migration-export-base":
				return fake_task(
					stdout='ATLAS_RESULT={"nbd_port": 10003, "nbd_pid": 5252, '
					'"base_size_bytes": 1, "meta_port": 10004, "meta_pid": 5253, '
					'"meta_size_bytes": 4096}'
				)
			if script == "migration-poll-hydration":
				return fake_task(stdout='ATLAS_RESULT={"hydration_percent": 100}')
			return fake_task(stdout="ok")

		with patch.object(migration_module, "run_task", side_effect=_fake_run_task):
			# Pending → the line names the source and the stop.
			migration_module.advance_migration(row)
			row.reload()
			self.assertTrue(row.progress_detail)
			self.assertIn("mig-source", row.progress_detail)

	def test_syncable_image_skips_the_base_ship(self) -> None:
		# A normal (syncable) image must NOT trigger any base-ship scripts — that path
		# is handled by clone-target's own presence pre-flight.
		vm = make_virtual_machine(self.source, make_image("mig-syncable-image-2").name, status="Stopped")
		doc = frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"virtual_machine": vm.name,
				"target_server": self.target,
			}
		)
		doc.keep_address = 0
		doc.forward_address = 0
		doc.flags.keep_address_forced = True
		row = doc.insert(ignore_permissions=True)

		scripts_seen: list[str] = []

		def _fake_run_task(*, script, variables, server, virtual_machine, timeout_seconds):
			scripts_seen.append(script)
			if script == "migration-export-source":
				return fake_task(
					stdout='ATLAS_RESULT={"nbd_port": 10001, "nbd_pid": 4242, '
					'"root_size_bytes": 1, "data_size_bytes": 0}'
				)
			if script == "migration-poll-hydration":
				return fake_task(stdout='ATLAS_RESULT={"hydration_percent": 100}')
			return fake_task(stdout="ok")

		from atlas.atlas import proxy as proxy_module

		with (
			patch.object(migration_module, "run_task", side_effect=_fake_run_task),
			patch.object(proxy_module, "reconcile_proxies", return_value=[]),
		):
			for _ in range(9):
				row.reload()
				migration_module.advance_migration(row)
			row.reload()

		self.assertEqual(row.status, "Done")
		self.assertNotIn("migration-export-base", scripts_seen)
		self.assertNotIn("migration-receive-base", scripts_seen)
		self.assertEqual(row.base_ship_state or "", "")


class TestCollapseForward(IntegrationTestCase):
	"""The operator-initiated Collapse-forward (spec/24 §2.9.5): tear the tunnel
	down on both hosts and fall the VM back to a fresh /128 (change-address)."""

	def setUp(self) -> None:
		self.source = _source_server()
		self.target = _target_server()
		self.image = make_image("mig-test-image").name
		for name in frappe.get_all("Virtual Machine", pluck="name"):
			frappe.delete_doc("Virtual Machine", name, force=1, ignore_permissions=True)

	def _forwarded_vm(self):
		"""A VM as it looks AFTER a keep-address migration: living on the target,
		still on its original /128, forwarded from the source."""
		vm = make_virtual_machine(self.target, self.image, status="Running")
		vm.flags.migrating = True
		vm.ipv6_address = "2001:db8:9::5"  # a source-range /128 it kept
		vm.traffic_forwarded_from = self.source
		vm.traffic_forwarded_since = frappe.utils.now_datetime()
		vm.save(ignore_permissions=True)
		return vm

	def test_collapse_forward_tears_down_and_reallocates(self) -> None:
		vm = self._forwarded_vm()
		old_ipv6 = vm.ipv6_address
		down_calls: list[str] = []

		def _fake_run_task(*, script, variables, server, virtual_machine, timeout_seconds):
			if script == "migration-forward-down":
				down_calls.append(f"{variables['ROLE']}@{server}")
			return fake_task(stdout="ok")

		from atlas.atlas import proxy as proxy_module

		with (
			patch.object(migration_module, "run_task", side_effect=_fake_run_task),
			# collapse_forward now stops the live VM first (so the disk converges off
			# any dm-clone before the re-provision); vm.stop() runs a host Task through
			# the VM module's run_task, so mock that too.
			patch.object(vm_module, "run_task", side_effect=_fake_run_task),
			patch.object(proxy_module, "reconcile_proxies", return_value=[]),
		):
			vm.collapse_forward()

		vm.reload()
		# The tunnel was torn down on BOTH ends.
		self.assertIn(f"target@{self.target}", down_calls)
		self.assertIn(f"source@{self.source}", down_calls)
		# A fresh /128 on the current (target) host, forward markers cleared.
		self.assertNotEqual(vm.ipv6_address, old_ipv6)
		self.assertTrue(str(vm.ipv6_address).startswith("2001:db8:a::"))
		self.assertFalse(vm.traffic_forwarded_from)
		self.assertFalse(vm.traffic_forwarded_since)

	def test_collapse_forward_refused_without_active_forward(self) -> None:
		vm = make_virtual_machine(self.target, self.image, status="Running")
		with self.assertRaisesRegex(frappe.ValidationError, "no active forward"):
			vm.collapse_forward()
