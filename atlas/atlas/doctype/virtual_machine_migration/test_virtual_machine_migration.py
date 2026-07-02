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

	def _row(self, vm):
		return frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"virtual_machine": vm.name,
				"target_server": self.target,
			}
		).insert(ignore_permissions=True)

	def test_phases_advance_in_order(self) -> None:
		vm = self._vm()
		row = self._row(vm)

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
