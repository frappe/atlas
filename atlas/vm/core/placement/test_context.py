import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid7

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from atlas.vm.core.placement.context import (
	CAPACITY_MAXIMUM_AGE,
	LOCK_WAIT_MAXIMUM_SECONDS,
	PLACEMENT_RATE_WINDOW,
	PlacementContext,
	is_pool_known_full,
	remember_pool_is_full,
)
from atlas.vm.core.placement.models import CurrentPlacement, PlacementRequirements, Resources

NOW = datetime(2026, 9, 17, 12)


def host_row(name: str = "a", **overrides: object) -> frappe._dict:
	"""Return one aggregated capacity row as the placement query reports it."""
	row = frappe._dict(
		name=name,
		architecture="amd64",
		is_sleepy_vm_host=0,
		sample_created_at=NOW,
		total_cpu_millicores=1000,
		total_memory_mib=4096,
		total_storage_mib=20480,
		free_cpu_millicores=1000,
		free_memory_mib=4096,
		free_storage_mib=20480,
		tenant_vm_count=0,
		sleepy_reserved_memory_mib=0,
		placement_count=0,
	)
	row.update(overrides)
	return row


def _raise(error: Exception):
	raise error


class TestPlacementContext(UnitTestCase):
	def setUp(self) -> None:
		read_committed = patch("atlas.vm.core.placement.context.is_read_committed", return_value=True)
		read_committed.start()
		self.addCleanup(read_committed.stop)
		clock = patch("atlas.vm.core.placement.context.now_datetime", return_value=NOW)
		clock.start()
		self.addCleanup(clock.stop)

	@staticmethod
	def requirements(**overrides: object) -> PlacementRequirements:
		values: dict[str, object] = {
			"cpu_millicores": 1000,
			"memory_mib": 1024,
			"disk_mib": 10240,
			"architecture": "amd64",
			"tenant_id": 7,
			"is_sleepy": False,
		}
		values.update(overrides)
		return PlacementRequirements(**values)  # type: ignore[arg-type]

	def context(self, rows: object, **kwargs: object) -> PlacementContext:
		with patch("atlas.vm.core.placement.context.frappe.db.sql", side_effect=rows):
			return PlacementContext(self.requirements(), 1.0, **kwargs)  # type: ignore[arg-type]

	def test_a_cached_snapshot_is_shared_between_placements(self) -> None:
		rows = [host_row("a")]
		store: dict[str, object] = {}
		cache = SimpleNamespace(
			get_value=lambda key: store.get(key),
			set_value=lambda key, value, expires_in_sec: store.__setitem__(key, value),
		)
		with (
			patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=rows) as sql,
			patch("atlas.vm.core.placement.context.frappe.cache", return_value=cache),
		):
			first = PlacementContext(self.requirements(), 1.0, cache_snapshot=True)
			second = PlacementContext(self.requirements(), 1.0, cache_snapshot=True)

		sql.assert_called_once()
		self.assertEqual(first.usage.hosts, second.usage.hosts)
		self.assertEqual(list(store), ["atlas:placement-snapshot:amd64:0:7"])

	def test_a_cached_snapshot_is_separate_for_each_pool_and_tenant(self) -> None:
		store: dict[str, object] = {}
		cache = SimpleNamespace(
			get_value=lambda key: store.get(key),
			set_value=lambda key, value, expires_in_sec: store.__setitem__(key, value),
		)
		with (
			patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=[host_row("a")]),
			patch("atlas.vm.core.placement.context.frappe.cache", return_value=cache),
		):
			PlacementContext(self.requirements(), 1.0, cache_snapshot=True)
			PlacementContext(self.requirements(tenant_id=9), 1.0, cache_snapshot=True)
			PlacementContext(self.requirements(is_sleepy=True), 1.0, cache_snapshot=True)

		self.assertEqual(
			sorted(store),
			[
				"atlas:placement-snapshot:amd64:0:7",
				"atlas:placement-snapshot:amd64:0:9",
				"atlas:placement-snapshot:amd64:1:7",
			],
		)

	def test_a_snapshot_is_read_fresh_unless_caching_is_asked_for(self) -> None:
		with (
			patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=[host_row("a")]) as sql,
			patch("atlas.vm.core.placement.context.frappe.cache") as cache,
		):
			PlacementContext(self.requirements(), 1.0)
			PlacementContext(self.requirements(), 1.0)

		self.assertEqual(sql.call_count, 2)
		cache.assert_not_called()

	def test_a_full_pool_is_remembered_for_an_equal_or_larger_shape(self) -> None:
		store: dict[str, object] = {}
		cache = SimpleNamespace(
			get_value=lambda key: store.get(key),
			set_value=lambda key, value, expires_in_sec: store.__setitem__(key, value),
		)
		with patch("atlas.vm.core.placement.context.frappe.cache", return_value=cache):
			remember_pool_is_full(self.requirements(memory_mib=2048, disk_mib=10240))

			self.assertTrue(is_pool_known_full(self.requirements(memory_mib=2048, disk_mib=10240)))
			self.assertTrue(is_pool_known_full(self.requirements(memory_mib=4096, disk_mib=20480)))
			self.assertFalse(is_pool_known_full(self.requirements(memory_mib=1024, disk_mib=10240)))
			self.assertFalse(is_pool_known_full(self.requirements(memory_mib=2048, disk_mib=5120)))
			self.assertFalse(is_pool_known_full(self.requirements(is_sleepy=True)))

	def test_a_full_pool_is_forgotten_when_the_entry_expires(self) -> None:
		store: dict[str, object] = {}
		cache = SimpleNamespace(
			get_value=lambda key: store.get(key),
			set_value=lambda key, value, expires_in_sec: store.__setitem__(key, value),
		)
		with patch("atlas.vm.core.placement.context.frappe.cache", return_value=cache):
			remember_pool_is_full(self.requirements())
			self.assertTrue(is_pool_known_full(self.requirements()))

			for entry in store.values():
				entry["until"] = time.time() - 1

			self.assertFalse(is_pool_known_full(self.requirements()))

	def test_dedicated_sleepy_hosts_keep_the_pools_apart(self) -> None:
		with patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=[]) as sql:
			PlacementContext(self.requirements(), 1.0)

		query, values = sql.call_args.args
		self.assertIn("server.is_sleepy_vm_host = %(is_sleepy)s", query)
		self.assertEqual(values["is_sleepy"], 0)

	def test_one_pool_drops_the_sleepy_host_filter(self) -> None:
		with patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=[]) as sql:
			placement = PlacementContext(self.requirements(), 1.0, use_dedicated_sleepy_vm_hosts=False)

		self.assertNotIn("server.is_sleepy_vm_host = %(is_sleepy)s", sql.call_args.args[0])

		with patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=[]) as capacity_sql:
			placement._host_has_capacity("a")

		self.assertNotIn("server.is_sleepy_vm_host = %(is_sleepy)s", capacity_sql.call_args.args[0])

	def test_one_pool_uses_its_own_snapshot_cache_entry(self) -> None:
		store: dict[str, object] = {}
		cache = SimpleNamespace(
			get_value=lambda key: store.get(key),
			set_value=lambda key, value, expires_in_sec: store.__setitem__(key, value),
		)
		with (
			patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=[host_row("a")]),
			patch("atlas.vm.core.placement.context.frappe.cache", return_value=cache),
		):
			PlacementContext(self.requirements(), 1.0, cache_snapshot=True)
			PlacementContext(
				self.requirements(), 1.0, cache_snapshot=True, use_dedicated_sleepy_vm_hosts=False
			)

		self.assertEqual(
			sorted(store),
			["atlas:placement-snapshot:amd64:0:7", "atlas:placement-snapshot:amd64:any:7"],
		)

	def test_placement_refuses_a_repeatable_read_transaction(self) -> None:
		with (
			patch("atlas.vm.core.placement.context.is_read_committed", return_value=False),
			self.assertRaisesRegex(RuntimeError, "READ COMMITTED"),
		):
			PlacementContext(self.requirements(), 1.0)

	def test_host_locks_are_scoped_to_the_site_database(self) -> None:
		with patch.object(frappe.db, "cur_db_name", "site_a"):
			first_site_lock = PlacementContext._host_lock_name("shared-host")
		with patch.object(frappe.db, "cur_db_name", "site_b"):
			second_site_lock = PlacementContext._host_lock_name("shared-host")

		self.assertNotEqual(first_site_lock, second_site_lock)
		self.assertLessEqual(len(first_site_lock), 64)

	def test_snapshot_query_filters_the_required_pool_and_sample_age(self) -> None:
		with patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=[]) as sql:
			placement = PlacementContext(self.requirements(is_sleepy=True), 1.0)

		query, values = sql.call_args.args
		self.assertNotIn("FOR UPDATE", query)
		self.assertEqual(values["architecture"], "amd64")
		self.assertEqual(values["is_sleepy"], 1)
		self.assertEqual(values["tenant_id"], 7)
		self.assertEqual(values["capacity_cutoff"], NOW - CAPACITY_MAXIMUM_AGE)
		self.assertEqual(values["rate_cutoff"], NOW - PLACEMENT_RATE_WINDOW)
		self.assertEqual(placement.usage.hosts, ())
		self.assertEqual(placement.usage.total, Resources(0, 0, 0))

	def test_snapshot_aggregates_hosts_and_drops_excluded_servers(self) -> None:
		rows = [
			host_row("a", free_memory_mib=3000, free_storage_mib=15000),
			host_row("b", total_memory_mib=8192, free_memory_mib=6000, tenant_vm_count=2),
			host_row("excluded"),
		]
		placement = self.context([rows], exclude_servers={"excluded"})

		self.assertEqual([host.name for host in placement.usage.hosts], ["a", "b"])
		self.assertEqual(placement.usage.total, Resources(2000, 12288, 40960))
		self.assertEqual(placement.usage.free, Resources(2000, 9000, 35480))
		self.assertEqual(placement.usage.tenant_vm_count, 2)
		self.assertEqual(placement.action, "migration")

	def test_rate_comes_from_the_snapshot_without_another_query(self) -> None:
		rows = [host_row("a", placement_count=2), host_row("b", placement_count=1)]
		with patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=rows) as sql:
			placement = PlacementContext(self.requirements(), 1.0)

			self.assertEqual(sql.call_args.args[1]["rate_cutoff"], NOW - timedelta(minutes=5))
			self.assertEqual(placement.get_placement_rate(), 0.6)
			self.assertEqual(placement.get_placement_rate("a"), 0.4)
			self.assertEqual(placement.get_placement_rate("b"), 0.2)
			self.assertEqual(placement.get_placement_rate("missing"), 0.0)

		sql.assert_called_once()

	def test_select_holds_the_host_lock_after_the_capacity_check(self) -> None:
		placement = self.context([[host_row("a")]])
		lock_name = placement._host_lock_name("a")
		with (
			patch.object(placement, "_acquire_host_lock", return_value=True) as acquire,
			patch.object(placement, "_host_has_capacity", return_value=True) as capacity,
			patch.object(placement, "_keep_host_lock_for_transaction") as hold,
		):
			self.assertTrue(placement.try_select("a"))

		acquire.assert_called_once_with(lock_name, wait=False)
		capacity.assert_called_once_with("a")
		hold.assert_called_once_with(lock_name)
		self.assertEqual(placement.selected_host, "a")
		self.assertFalse(placement.has_contended_hosts)

	def test_a_host_without_capacity_releases_its_lock(self) -> None:
		placement = self.context([[host_row("a")]])
		lock_name = placement._host_lock_name("a")
		with (
			patch.object(placement, "_acquire_host_lock", return_value=True),
			patch.object(placement, "_host_has_capacity", return_value=False),
			patch.object(placement, "_release_host_lock") as release,
		):
			self.assertFalse(placement.try_select("a"))

		release.assert_called_once_with(lock_name)
		self.assertFalse(placement.has_contended_hosts)
		self.assertEqual(placement.probe_count, 1)

	def test_every_probed_host_is_counted(self) -> None:
		placement = self.context([[host_row("busy"), host_row("full")]])
		with (
			patch.object(placement, "_acquire_host_lock", side_effect=[False, True]),
			patch.object(placement, "_host_has_capacity", return_value=False),
			patch.object(placement, "_release_host_lock"),
		):
			self.assertFalse(placement.try_select("busy"))
			self.assertFalse(placement.try_select("full"))

		self.assertTrue(placement.has_contended_hosts)
		self.assertEqual(placement.probe_count, 2)

	def test_a_host_outside_the_snapshot_is_not_a_probe(self) -> None:
		placement = self.context([[host_row("a")]], exclude_servers={"excluded"})
		with patch.object(placement, "_acquire_host_lock") as acquire:
			self.assertFalse(placement.try_select("excluded"))
			self.assertFalse(placement.try_select("missing"))

		acquire.assert_not_called()
		self.assertEqual(placement.probe_count, 0)

	def test_capacity_failure_releases_the_host_lock(self) -> None:
		placement = self.context([[host_row("a")]])
		with (
			patch.object(placement, "_acquire_host_lock", return_value=True),
			patch.object(placement, "_host_has_capacity", side_effect=RuntimeError("database fault")),
			patch.object(placement, "_release_host_lock") as release,
			self.assertRaisesRegex(RuntimeError, "database fault"),
		):
			placement.try_select("a")

		release.assert_called_once()

	def test_a_waiting_selection_uses_the_remaining_lock_budget(self) -> None:
		placement = self.context([[host_row("a")]])
		with patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=[(1,)]) as sql:
			self.assertTrue(placement._acquire_host_lock("lock-name", wait=True))

		self.assertEqual(sql.call_args.args, ("SELECT GET_LOCK(%s, %s)", ("lock-name", 0.15)))

	def test_a_waiting_selection_gives_up_within_the_remaining_budget(self) -> None:
		placement = self.context([[host_row("a")]])
		with patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=[(0,)]) as sql:
			self.assertFalse(placement.try_select("a", wait=True))

		self.assertEqual(sql.call_args.args[1][1], LOCK_WAIT_MAXIMUM_SECONDS)
		self.assertTrue(placement.has_contended_hosts)

	def test_a_stuck_lock_holder_reports_contention_instead_of_failing(self) -> None:
		placement = self.context([[host_row("a")]])
		with patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=[(0,)]):
			self.assertFalse(placement.try_select("a", wait=True))

		self.assertTrue(placement.has_contended_hosts)
		self.assertIsNone(placement.selected_host)

	def test_a_database_fault_while_locking_is_not_hidden(self) -> None:
		placement = self.context([[host_row("a")]])
		with (
			patch(
				"atlas.vm.core.placement.context.frappe.db.sql",
				side_effect=lambda *a, **k: _raise(
					frappe.db.OperationalError(2006, "MySQL server has gone away")
				),
			),
			self.assertRaises(frappe.db.OperationalError),
		):
			placement.try_select("a", wait=True)

	def test_a_missing_lock_is_an_error_during_placement(self) -> None:
		with (
			patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=[(None,)]),
			self.assertRaisesRegex(RuntimeError, "RELEASE_LOCK returned None"),
		):
			PlacementContext._release_host_lock("missing-lock")

	def test_a_lost_lock_after_the_transaction_is_logged_without_raising(self) -> None:
		with (
			patch.object(
				PlacementContext,
				"_release_host_lock",
				side_effect=RuntimeError("connection changed"),
			),
			patch("atlas.vm.core.placement.context.frappe.logger") as logger,
		):
			PlacementContext._release_host_lock_after_transaction("lost-lock")

		logger.return_value.exception.assert_called_once_with(
			"Failed to release placement lock %s after the transaction ended", "lost-lock"
		)

	def test_an_exhausted_budget_never_queues_on_a_host(self) -> None:
		with patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=[host_row("a")]):
			placement = PlacementContext(self.requirements(), 1.0, deadline=time.monotonic() - 1)

		with patch("atlas.vm.core.placement.context.frappe.db.sql") as sql:
			self.assertFalse(placement.try_select("a", wait=True))

		sql.assert_not_called()

	def test_select_reports_contention_instead_of_waiting_for_the_lock(self) -> None:
		placement = self.context([[host_row("a")]])
		with patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=[(0,)]):
			self.assertFalse(placement.try_select("a"))

		self.assertTrue(placement.has_contended_hosts)

	def test_select_reports_a_database_lock_error(self) -> None:
		placement = self.context([[host_row("a")]])
		with (
			patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=[(None,)]),
			self.assertRaisesRegex(RuntimeError, "GET_LOCK returned NULL"),
		):
			placement.try_select("a")

		self.assertFalse(placement.has_contended_hosts)

	def test_select_ignores_an_unknown_or_excluded_host(self) -> None:
		placement = self.context([[host_row("a")]], exclude_servers={"excluded"})
		with patch("atlas.vm.core.placement.context.frappe.db.sql") as sql:
			self.assertFalse(placement.try_select("excluded"))
			self.assertFalse(placement.try_select("missing"))

		sql.assert_not_called()
		self.assertFalse(placement.has_contended_hosts)

	def test_select_accepts_memory_and_storage_fit_with_oversubscribed_cpu(self) -> None:
		placement = self.context([[host_row("a", free_cpu_millicores=0)]])
		with patch("atlas.vm.core.placement.context.frappe.db.sql", return_value=[["a"]]) as sql:
			self.assertTrue(placement._host_has_capacity("a"))

		self.assertNotIn("available_cpu_millicores", sql.call_args.args[0])

	def test_a_current_placement_marks_the_operation_as_a_resize(self) -> None:
		placement = self.context(
			[[host_row("a")]], current_placement=CurrentPlacement("a", memory_mib=1024, disk_mib=1024)
		)

		self.assertEqual(placement.action, "resize")
		self.assertEqual(placement.current_host_name, "a")

	def test_a_second_selection_is_a_programming_error(self) -> None:
		placement = self.context([[host_row("a")]])
		with (
			patch.object(placement, "_acquire_host_lock", return_value=True),
			patch.object(placement, "_host_has_capacity", return_value=True),
			patch.object(placement, "_keep_host_lock_for_transaction"),
		):
			self.assertTrue(placement.try_select("a"))
			with self.assertRaisesRegex(RuntimeError, "already selected"):
				placement.try_select("a")


class TestPlacementLockQuery(IntegrationTestCase):
	def setUp(self) -> None:
		super().setUp()
		with self.secondary_connection():
			secondary_connection_id = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]

		# A Metal Server name column is a UUID, so it rejects any other value.
		self.host_name = str(uuid7())
		self.sample_name = f"test-usage-{frappe.generate_hash(length=8)}"
		with self.primary_connection():
			primary_connection_id = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
			frappe.db.sql(
				"""
				INSERT INTO `tabMetal Server`
					(name, creation, modified, owner, modified_by, status,
					 is_provisioning_completed, architecture, is_sleepy_vm_host)
				VALUES
					(%(name)s, %(now)s, %(now)s, 'Administrator', 'Administrator',
					 'Running', 1, 'amd64', 0)
				""",
				{"name": self.host_name, "now": NOW},
			)
			frappe.db.sql(
				"""
				INSERT INTO `tabMetal Server Usage`
					(name, creation, modified, owner, modified_by, server,
					 available_memory_mib, available_storage_mib)
				VALUES
					(%(name)s, %(now)s, %(now)s, 'Administrator', 'Administrator',
					 %(server)s, 4096, 20480)
				""",
				{"name": self.sample_name, "server": self.host_name, "now": NOW},
			)

		self.assertNotEqual(primary_connection_id, secondary_connection_id)

	def placement(
		self, current_placement: CurrentPlacement | None = None, **overrides: object
	) -> PlacementContext:
		placement = object.__new__(PlacementContext)
		placement.requirements = TestPlacementContext.requirements(**overrides)
		placement.current_placement = current_placement
		placement._created_at = NOW
		placement._deadline = None
		placement._excluded_servers = frozenset()
		placement._selected_host = None
		placement.has_contended_hosts = False
		placement.last_probe_was_contended = False
		placement.probe_count = 0
		placement.use_dedicated_sleepy_vm_hosts = True
		placement.usage = PlacementContext._build_fleet_usage(placement, [host_row(self.host_name)])
		return placement

	def test_capacity_query_accepts_a_fitting_host(self) -> None:
		with self.primary_connection():
			self.assertTrue(self.placement()._host_has_capacity(self.host_name))

	def test_capacity_query_rejects_a_host_without_capacity(self) -> None:
		with self.primary_connection():
			self.assertFalse(self.placement(memory_mib=8192)._host_has_capacity(self.host_name))

	def test_the_current_host_of_a_resize_needs_room_only_for_the_increase(self) -> None:
		current_placement = CurrentPlacement(self.host_name, memory_mib=2048, disk_mib=0)
		with self.primary_connection():
			self.assertTrue(
				self.placement(current_placement, memory_mib=6144)._host_has_capacity(self.host_name)
			)
			self.assertFalse(self.placement(memory_mib=6144)._host_has_capacity(self.host_name))

	def test_a_resize_migration_reserves_its_target_shape(self) -> None:
		virtual_machine_name = f"test-vm-{frappe.generate_hash(length=8)}"
		with self.primary_connection():
			frappe.db.sql(
				"""
				INSERT INTO `tabVirtual Machine`
					(name, creation, modified, owner, modified_by, memory_mib, disk_mib)
				VALUES
					(%(name)s, %(created)s, %(created)s, 'Administrator', 'Administrator', 1024, 1024)
				""",
				{"name": virtual_machine_name, "created": NOW - timedelta(days=1)},
			)
			frappe.db.sql(
				"""
				INSERT INTO `tabVirtual Machine Migration`
					(name, creation, modified, owner, modified_by, virtual_machine, status,
					 destination_metal_server, target_memory_mib, target_disk_mib)
				VALUES
					(%(name)s, %(now)s, %(now)s, 'Administrator', 'Administrator', %(virtual_machine)s,
					 'copying', %(server)s, 4096, 1024)
				""",
				{
					"name": str(uuid7()),
					"now": NOW,
					"virtual_machine": virtual_machine_name,
					"server": self.host_name,
				},
			)

			self.assertFalse(self.placement(memory_mib=1024)._host_has_capacity(self.host_name))

	def test_rejected_host_releases_its_placement_lock(self) -> None:
		placement = self.placement(memory_mib=8192)
		with self.primary_connection():
			self.assertFalse(placement.try_select(self.host_name))

		with self.secondary_connection():
			lock_name = placement._host_lock_name(self.host_name)
			try:
				self.assertEqual(frappe.db.sql("SELECT GET_LOCK(%s, 0)", lock_name), ((1,),))
			finally:
				frappe.db.sql("SELECT RELEASE_LOCK(%s)", lock_name)

	def test_selected_host_keeps_its_lock_until_rollback(self) -> None:
		placement = self.placement()
		lock_name = placement._host_lock_name(self.host_name)
		with self.primary_connection():
			self.assertTrue(placement.try_select(self.host_name))

			with self.secondary_connection():
				self.assertEqual(frappe.db.sql("SELECT GET_LOCK(%s, 0)", lock_name), ((0,),))

			frappe.db.rollback()

			with self.secondary_connection():
				try:
					self.assertEqual(frappe.db.sql("SELECT GET_LOCK(%s, 0)", lock_name), ((1,),))
				finally:
					frappe.db.sql("SELECT RELEASE_LOCK(%s)", lock_name)


class TestPlacementLockCommit(IntegrationTestCase):
	def test_commit_releases_the_selected_host_lock(self) -> None:
		with self.secondary_connection():
			secondary_connection_id = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]

		lock_name = f"atlas:test-placement:{frappe.generate_hash(length=16)}"
		with self.primary_connection():
			primary_connection_id = frappe.db.sql("SELECT CONNECTION_ID()")[0][0]
			self.assertEqual(frappe.db.sql("SELECT GET_LOCK(%s, 0)", lock_name), ((1,),))
			PlacementContext._keep_host_lock_for_transaction(lock_name)

			with self.secondary_connection():
				self.assertEqual(frappe.db.sql("SELECT GET_LOCK(%s, 0)", lock_name), ((0,),))

			frappe.db.commit()  # nosemgrep

			with self.secondary_connection():
				try:
					self.assertEqual(frappe.db.sql("SELECT GET_LOCK(%s, 0)", lock_name), ((1,),))
				finally:
					frappe.db.sql("SELECT RELEASE_LOCK(%s)", lock_name)

		self.assertNotEqual(primary_connection_id, secondary_connection_id)
