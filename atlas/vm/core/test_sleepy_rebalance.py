from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from atlas.vm.core.metal_models import MetalVirtualMachine
from atlas.vm.core.placement.transaction import start_read_committed_transaction
from atlas.vm.core.sleepy_rebalance import SleepyHostRebalance, rebalance_sleepy_host
from atlas.vm.core.test_metal_models import complete_response
from atlas.vm.core.vm_migration import MigrationService

MODULE = "atlas.vm.core.sleepy_rebalance"


class TestSleepyRebalance(IntegrationTestCase):
	def policy(self) -> SleepyHostRebalance:
		return SleepyHostRebalance(self.source, allowed_destinations={self.target})

	def setUp(self) -> None:
		start_read_committed_transaction()
		self.addCleanup(frappe.db.rollback)
		self.source = self.insert_host()
		self.target = self.insert_host()
		self.information: dict[str, MetalVirtualMachine] = {}
		self.base = self.insert_vm(8500, state="running", sleepy=False)
		client = self.enterContext(patch(f"{MODULE}.MetalClient"))
		client.return_value.get_virtual_machine.side_effect = self.information.__getitem__

	def insert_host(self) -> str:
		report = {
			"architecture": "amd64",
			"cpu_profile": "test",
			"cpu_vendor": "test",
			"cpu_model": "test",
			"cpu_features": "test",
			"snapshot_host": "test",
			"tsc_khz": 1000000,
			"tsc_scaling": True,
		}
		host = frappe.get_doc(
			{
				"doctype": "Metal Server",
				"name": str(uuid4()),
				"title": str(uuid4()),
				"architecture": "amd64",
				"status": "Running",
				"is_sleepy_vm_host": 1,
				"is_provisioning_completed": 1,
				"host_compatibility_report": frappe.as_json(report),
			}
		)
		host.db_insert()
		frappe.get_doc(
			{
				"doctype": "Metal Server Usage",
				"name": str(uuid4()),
				"server": host.name,
				"total_memory_mib": 10000,
				"available_memory_mib": 10000,
				"total_storage_mib": 100000,
				"available_storage_mib": 100000,
				"total_cpu_millicores": 10000,
				"available_cpu_millicores": 10000,
			}
		).db_insert()
		return host.name

	def insert_vm(
		self,
		memory: int,
		state: str = "stopped",
		sleep_state: str = "sleeping",
		*,
		sleepy: bool = True,
		server: str | None = None,
		**values,
	) -> str:
		machine = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"name": str(uuid4()),
				"server": server or self.source,
				"memory_mib": memory,
				"disk_mib": 16,
				"cpu_millicores": 1000,
				"tenant_id": 99,
				"sleep_after_idle_seconds": 10 if sleepy else 0,
				**values,
			}
		)
		machine.db_insert()
		frappe.get_doc(
			{
				"doctype": "Virtual Machine State",
				"name": machine.name,
				"virtual_machine": machine.name,
				"server": machine.server,
				"status": state,
				"synced_at": now_datetime(),
			}
		).db_insert()
		response = complete_response()
		response["id"] = machine.name
		response["desired"]["compute"]["sleep_after_idle_seconds"] = 10 if sleepy else 0
		response["observed"].update(
			{"state": state, "sleep_state": sleep_state if state == "stopped" else ""}
		)
		self.information[machine.name] = MetalVirtualMachine.from_dict(response)
		return machine.name

	def record_move(self, machine: str, status: str = "completed", days_ago: int = 0) -> None:
		frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"name": str(uuid4()),
				"virtual_machine": machine,
				"source_metal_server": self.source,
				"destination_metal_server": self.target,
				"status": status,
				"finished_at": now_datetime() - timedelta(days=days_ago),
			}
		).db_insert()

	def test_85_percent_includes_every_sleep_state_but_not_running_or_paused(self) -> None:
		sleepers = [
			self.insert_vm(100, sleep_state=state) for state in ("sleeping", "deep-sleeping", "comatose")
		]
		running = self.insert_vm(200, state="running")
		paused = self.insert_vm(100, state="paused")
		frappe.db.set_value("Virtual Machine", self.base, "memory_mib", 8200)
		candidates = {machine.name for machine in self.policy().candidates}
		self.assertEqual(candidates, set(sleepers))
		self.assertNotIn(running, candidates)
		self.assertNotIn(paused, candidates)
		frappe.db.set_value("Virtual Machine", self.base, "memory_mib", 8199)
		self.assertEqual(self.policy().candidates, [])

	def test_running_requires_strictly_more_than_90_percent(self) -> None:
		running = self.insert_vm(500, state="running")
		self.assertEqual(self.policy().candidates, [])
		frappe.db.set_value("Virtual Machine", self.base, "memory_mib", 8501)
		self.assertEqual([machine.name for machine in self.policy().candidates], [running])

	def test_running_priority_counts_only_completed_moves_in_the_last_day(self) -> None:
		small = self.insert_vm(500, state="running")
		large = self.insert_vm(1500, state="running")
		recent = self.insert_vm(2000, state="running")
		self.record_move(small, status="failed")
		self.record_move(large, days_ago=2)
		self.record_move(recent)
		self.assertEqual([machine.name for machine in self.policy().candidates], [large, small, recent])

	def test_running_selection_stops_at_projected_85_percent(self) -> None:
		first, second = (self.insert_vm(500, state="running") for _ in range(2))
		recent = self.insert_vm(2500, state="running")
		self.record_move(recent)
		frappe.db.set_value("Virtual Machine", self.base, "memory_mib", 6000)
		selected, targets = self.policy().plan()
		self.assertEqual(set(selected), {first, second})
		self.assertEqual(targets, [self.target])

	def test_selects_more_than_eight_sleepers_largest_first(self) -> None:
		sleepers = [self.insert_vm(memory) for memory in range(100, 110)]
		selected, _targets = self.policy().plan()
		self.assertEqual(selected, sleepers[::-1])

	def test_excludes_stopped_protected_terminating_and_migrating_virtual_machines(self) -> None:
		self.insert_vm(100, sleep_state="")
		self.insert_vm(100, is_termination_protected=1)
		self.insert_vm(100, is_terminating=1)
		self.insert_vm(100, is_draft=1)
		self.insert_vm(100, active_migration=str(uuid4()))
		self.assertEqual(self.policy().candidates, [])

	def test_proxy_is_excluded_even_without_termination_protection(self) -> None:
		proxy = self.insert_vm(100)
		frappe.get_doc(
			{"doctype": "Proxy Server", "name": str(uuid4()), "virtual_machine": proxy}
		).db_insert()
		self.assertEqual(self.policy().candidates, [])

	def test_stale_missing_and_wrong_host_states_do_not_count_as_free_memory(self) -> None:
		for values in (
			{"synced_at": now_datetime() - timedelta(minutes=3)},
			{"synced_at": now_datetime(), "server": self.target},
		):
			frappe.db.set_value("Virtual Machine State", self.base, values)
			self.assertIsNone(self.policy().memory_report(self.source))

	def test_stale_host_capacity_is_skipped(self) -> None:
		sample = frappe.db.get_value("Metal Server Usage", {"server": self.source}, "name")
		frappe.db.set_value("Metal Server Usage", sample, "creation", now_datetime() - timedelta(minutes=3))
		self.assertIsNone(self.policy().memory_report(self.source))

	def test_destination_must_be_a_ready_emptier_nonmaintenance_sleepy_host(self) -> None:
		machine = self.insert_vm(100)
		policy = self.policy()
		self.assertEqual([host.name for host in policy.destinations(policy.candidates[0])], [self.target])
		for field, value in (("is_in_maintenance", 1), ("is_sleepy_vm_host", 0), ("status", "Stopped")):
			with self.subTest(field=field):
				frappe.db.set_value("Metal Server", self.target, field, value)
				policy = self.policy()
				self.assertEqual(policy.destinations(policy.candidates[0]), [])
				frappe.db.set_value(
					"Metal Server",
					self.target,
					field,
					"Running" if field == "status" else 1 if field == "is_sleepy_vm_host" else 0,
				)
		self.assertEqual(machine, policy.candidates[0].name)

	def test_batch_accounts_for_combined_destination_memory(self) -> None:
		self.insert_vm(6000)
		self.insert_vm(4000)
		selected, _targets = self.policy().plan()
		self.assertEqual(len(selected), 1)

	def test_existing_incoming_migration_reserves_destination_capacity(self) -> None:
		self.insert_vm(7000)
		incoming = self.insert_vm(4000, state="running")
		self.record_move(incoming, status="preparing")
		migration = frappe.db.get_value("Virtual Machine Migration", {"virtual_machine": incoming}, "name")
		frappe.db.set_value("Virtual Machine", incoming, "active_migration", migration)
		self.assertEqual(self.policy().plan(), ([], []))

	def test_incompatible_destination_is_not_selected(self) -> None:
		self.insert_vm(100)
		frappe.db.set_value("Metal Server", self.target, "host_compatibility_report", None)
		self.assertEqual(self.policy().plan(), ([], []))

	def test_queued_move_rechecks_only_the_drain_destinations(self) -> None:
		machine = self.insert_vm(100)
		rebalance_sleepy_host(self.source)
		drain = frappe.db.get_value("Metal Server Drain", {"source_metal_server": self.source}, "name")
		self.insert_host()
		service = MigrationService(
			frappe.get_doc(
				{
					"doctype": "Virtual Machine Migration",
					"virtual_machine": machine,
					"source_metal_server": self.source,
					"metal_server_drain": drain,
				}
			)
		)
		self.assertEqual(service._load_allowed_destinations(), {self.target})
		frappe.db.set_value("Metal Server", self.target, "is_in_maintenance", 1)
		self.assertEqual(service._load_allowed_destinations(), set())

	def test_job_preserves_maintenance_and_submits_one_drain_with_four_slots(self) -> None:
		machine = self.insert_vm(100)
		frappe.db.set_value("Metal Server", self.source, "is_in_maintenance", 1)
		rebalance_sleepy_host(self.source)
		rebalance_sleepy_host(self.source)
		drains = frappe.get_all(
			"Metal Server Drain", filters={"source_metal_server": self.source}, pluck="name"
		)
		self.assertEqual(len(drains), 1)
		drain = frappe.get_doc("Metal Server Drain", drains[0])
		self.assertEqual(
			(drain.status, drain.is_sleepy_rebalance, drain.parallel_migrations), ("Running", 1, 4)
		)
		self.assertEqual((drain.has_memory_snapshot, drain.allow_disk_only_fallback), (1, 0))
		self.assertEqual(drain.retry_failed_migrations, 1)
		self.assertEqual([row.virtual_machine for row in drain.virtual_machines], [machine])
		self.assertEqual(frappe.db.get_value("Metal Server", self.source, "is_in_maintenance"), 1)

	def test_job_leaves_a_nonmaintenance_source_available(self) -> None:
		self.insert_vm(100)
		rebalance_sleepy_host(self.source)
		self.assertEqual(frappe.db.get_value("Metal Server", self.source, "is_in_maintenance"), 0)

	def test_automatic_drain_completes_when_a_sleeper_wakes_below_90_percent(self) -> None:
		machine = self.insert_vm(100)
		rebalance_sleepy_host(self.source)
		name = frappe.db.get_value("Metal Server Drain", {"source_metal_server": self.source}, "name")
		response = complete_response()
		response["id"] = machine
		response["observed"].update({"state": "running", "sleep_state": ""})
		self.information[machine] = MetalVirtualMachine.from_dict(response)
		frappe.get_doc("Metal Server Drain", name).run_next_step()
		self.assertEqual(frappe.db.get_value("Metal Server Drain", name, "status"), "Completed")

	def test_queued_automatic_migration_rechecks_source_before_placement(self) -> None:
		machine = self.insert_vm(100)
		rebalance_sleepy_host(self.source)
		name = frappe.db.get_value("Metal Server Drain", {"source_metal_server": self.source}, "name")
		migration = frappe.get_doc(
			{
				"doctype": "Virtual Machine Migration",
				"name": str(uuid4()),
				"virtual_machine": machine,
				"source_metal_server": self.source,
				"metal_server_drain": name,
				"status": "scheduled",
				"memory_snapshot_status": "not_started",
			}
		)
		migration.db_insert()
		response = complete_response()
		response["id"] = machine
		response["observed"].update({"state": "running", "sleep_state": ""})
		information = MetalVirtualMachine.from_dict(response)
		frappe.db.set_value("Virtual Machine", machine, "active_migration", migration.name)
		with patch("atlas.vm.core.vm_migration.MetalClient") as client, patch.object(frappe.db, "commit"):
			client.return_value.get_virtual_machine.return_value = information
			service = MigrationService(migration)
			self.assertFalse(service.select_destination_metal_server())
			self.assertEqual(service.migration.status, "aborted")
			self.assertIsNone(service.migration.destination_metal_server)
			self.assertIsNone(frappe.db.get_value("Virtual Machine", machine, "active_migration"))
