"""Unit coverage for VM migration (spec/19): the pure parse, the phase machine,
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


class TestAddressSchemeDerivation(IntegrationTestCase):
	"""The keep_address / forward_address derivation from the provider capability
	(spec/19 §2.8), independent of any live provider construction — the capability
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

		expected = [
			"ExportingSnapshot",
			"TargetPreparing",
			"InjectingIdentity",
			"Hydrating",
			"CutoverStarting",
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
		source (spec/19 §2.9)."""
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
			for _ in range(8):
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


class TestCollapseForward(IntegrationTestCase):
	"""The operator-initiated Collapse-forward (spec/19 §2.9.5): tear the tunnel
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
