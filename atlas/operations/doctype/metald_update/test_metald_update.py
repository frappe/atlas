# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

import json
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from atlas.atlas.core.artifacts import get_linked_files
from atlas.operations.doctype.metald_update.metald_update import (
	MetaldUpdate,
	create_metald_update,
	read_server_versions,
)
from atlas.vm.core.metal_client import MetalClientError

MODULE = "atlas.operations.doctype.metald_update.metald_update"
TARGET = {"metald_version": "2.0.0", "wg_mesh_version": "2.0.0", "firecracker_version": "v1.16.1"}
OLD = {"metald_version": "1.0.0", "wg_mesh_version": "1.0.0", "firecracker_version": "v1.16.1"}


def insert_server(title: str, status: str = "Running") -> str:
	name = str(uuid4())
	frappe.get_doc(
		{
			"doctype": "Metal Server",
			"name": name,
			"title": title,
			"status": status,
			"server_size": "test-size",
			"server_image": "test-image",
		}
	).db_insert()
	return name


def record_synchronization(server: str) -> None:
	frappe.get_doc(
		{
			"doctype": "Metal Server Usage",
			"server": server,
			"total_cpu_millicores": 1000,
			"available_cpu_millicores": 1000,
			"virtual_machine_count": 0,
			"total_memory_mib": 1024,
			"available_memory_mib": 1024,
			"total_storage_mib": 1024,
			"available_storage_mib": 1024,
		}
	).insert(ignore_permissions=True)


class TestMetaldUpdate(IntegrationTestCase):
	def setUp(self) -> None:
		# Each test starts its own update, and only one update can run at a time.
		self.addCleanup(frappe.db.rollback)
		for file_name in ("test-metald-binary", "test-wg-mesh-binary"):
			frappe.get_doc({"doctype": "File", "name": file_name, "file_name": file_name}).db_insert()
		for field, value in {
			"metald_version": "2.0.0",
			"metald_binary_x86_64_file": "test-metald-binary",
			"metald_binary_hash": "metald-hash",
			"wg_mesh_version": "2.0.0",
			"wg_mesh_binary_x86_64_file": "test-wg-mesh-binary",
			"wg_mesh_binary_hash": "wg-mesh-hash",
		}.items():
			frappe.db.set_single_value("Atlas Settings", field, value)

	def create_update(self, servers: list[str], **values: object) -> MetaldUpdate:
		return frappe.get_doc(
			{"doctype": "Metald Update", "servers": [{"server": server} for server in servers], **values}
		).insert()

	def plan(self, update: MetaldUpdate, versions_by_server: dict[str, object]) -> MetaldUpdate:
		"""Plan the update with the versions or error that each server reports."""

		def get_host_versions(server):
			result = versions_by_server[server.name]
			if isinstance(result, Exception):
				raise result
			return result

		update.plan()
		with patch(f"{MODULE}.get_host_versions", side_effect=get_host_versions):
			for row in update.servers:
				read_server_versions(update.name, row.name)
		update.run_next_step()
		update.reload()
		return update

	def start_planned(self, servers: list[str], **values: object) -> MetaldUpdate:
		update = self.create_update(servers, **values)
		update = self.plan(update, dict.fromkeys(servers, OLD))
		update.start()
		update.reload()
		return update

	def insert_ssh_task(self, server: str, status: str, output: str = "") -> str:
		task = frappe.get_doc(
			{
				"doctype": "SSH Task",
				"name": f"test-ssh-task-{uuid4().hex[:8]}",
				"target_type": "Metal Server",
				"target": server,
				"port": 22,
				"ssh_user": "root",
				"timeout_seconds": 60,
				"script": "true",
				"status": status,
				"output": output,
			}
		)
		task.db_insert()
		return task.name

	def test_a_new_update_pins_the_build_and_adds_every_running_server(self) -> None:
		running = insert_server("test-update-running")
		stopped = insert_server("test-update-stopped", status="Stopped")

		update = self.create_update([])

		servers = [row.server for row in update.servers]
		self.assertIn(running, servers)
		self.assertNotIn(stopped, servers)
		self.assertEqual({row.status for row in update.servers}, {"Pending"})
		self.assertEqual(update.wg_mesh_binary_hash, "wg-mesh-hash")
		self.assertIn("test-metald-binary", get_linked_files())

	def test_planning_records_versions_and_skips_servers_that_need_no_upgrade(self) -> None:
		outdated = insert_server("test-plan-outdated")
		current = insert_server("test-plan-current")
		unreachable = insert_server("test-plan-unreachable")
		broken = insert_server("test-plan-broken")
		update = self.create_update([outdated, current, unreachable, broken])

		with patch(f"{MODULE}.frappe.log_error"):
			update = self.plan(
				update,
				{
					outdated: OLD,
					current: TARGET,
					unreachable: MetalClientError("Metal is unreachable"),
					broken: RuntimeError("no TLS files"),
				},
			)

		self.assertEqual(update.status, "Planned")
		self.assertEqual(
			[(row.status, row.metald_version, row.firecracker_version) for row in update.servers],
			[
				("Pending", "1.0.0", "v1.16.1"),
				("Skipped", "2.0.0", "v1.16.1"),
				("Failed", None, None),
				("Failed", None, None),
			],
		)
		with self.assertRaises(frappe.ValidationError):
			update.start()

	def test_planning_completes_an_update_whose_servers_need_no_upgrade(self) -> None:
		servers = [insert_server("test-plan-done-a"), insert_server("test-plan-done-b")]
		update = self.create_update(servers)

		update = self.plan(update, dict.fromkeys(servers, TARGET))

		self.assertEqual(update.status, "Completed")
		self.assertEqual({row.status for row in update.servers}, {"Skipped"})

	def test_start_batches_only_the_servers_that_need_the_build(self) -> None:
		servers = [insert_server(f"test-update-batch-{index}") for index in range(5)]
		update = self.create_update(servers, canary_size=1, batch_size=2)
		versions = dict.fromkeys(servers, OLD) | {servers[1]: TARGET}
		update = self.plan(update, versions)

		update.start()

		update.reload()
		self.assertEqual([row.batch for row in update.servers], [1, 0, 2, 2, 3])
		self.assertEqual((update.status, update.current_batch), ("Running", 1))

	def test_a_step_starts_an_upgrade_task_and_links_it(self) -> None:
		update = self.start_planned([insert_server("test-update-start")])

		with (
			patch(f"{MODULE}.is_job_enqueued", return_value=False),
			patch(f"{MODULE}.HostInstallation") as host_installation,
		):
			host_installation.return_value.upgrade_metald.return_value.name = "test-ssh-task"
			update.run_next_step()

		update.reload()
		self.assertEqual(
			(update.servers[0].status, update.servers[0].ssh_task), ("Upgrading", "test-ssh-task")
		)
		host_installation.return_value.upgrade_metald.assert_called_once_with(update, run_in_background=True)

	def test_a_step_copies_the_upgrade_task_result_into_the_row(self) -> None:
		servers = [insert_server(f"test-update-task-{index}") for index in range(3)]
		update = self.start_planned(servers, canary_size=3)
		tasks = [
			self.insert_ssh_task(servers[0], "Success"),
			self.insert_ssh_task(
				servers[1], "Failed", "==> restart metal.service\nmetal.service did not start"
			),
			self.insert_ssh_task(servers[2], "Pending"),
		]
		for row, task in zip(update.servers, tasks, strict=True):
			row.db_set({"status": "Upgrading", "ssh_task": task})

		with patch(f"{MODULE}.is_job_enqueued", return_value=False):
			update.run_next_step()

		update.reload()
		self.assertEqual([row.status for row in update.servers], ["Upgraded", "Failed", "Failed"])
		self.assertIn("did not start", update.servers[1].status_reason)
		self.assertEqual(update.servers[2].status_reason, "The SSH Task did not start.")

	def test_a_failed_server_pauses_the_update_until_resume(self) -> None:
		update = self.start_planned([insert_server("test-update-failed")])
		update.servers[0].db_set({"status": "Failed", "status_reason": "test failure"})

		update.run_next_step()

		update.reload()
		self.assertEqual(update.status, "Paused")

		update.resume()

		update.reload()
		self.assertEqual((update.status, update.servers[0].status), ("Running", "Pending"))

	def test_a_failed_server_stops_the_upgrades_that_have_not_started(self) -> None:
		update = self.start_planned(
			[insert_server("test-stop-failed"), insert_server("test-stop-held")], canary_size=2
		)
		update.servers[0].db_set({"status": "Failed", "status_reason": "test failure"})

		with patch(f"{MODULE}.HostInstallation") as host_installation:
			update.run_next_step()

		update.reload()
		self.assertEqual((update.status, update.servers[1].status), ("Paused", "Pending"))
		host_installation.assert_not_called()

	def test_a_read_from_an_older_plan_leaves_the_row_alone(self) -> None:
		update = self.create_update([insert_server("test-read-old")])
		update.plan()
		row = update.servers[0]
		replacement = insert_server("test-read-new")

		def replan_during_the_read(server):
			frappe.db.set_value("Metald Update Server", row.name, "server", replacement)
			return OLD

		with patch(f"{MODULE}.get_host_versions", side_effect=replan_during_the_read):
			read_server_versions(update.name, row.name)

		self.assertEqual(frappe.db.get_value("Metald Update Server", row.name, "status"), "Planning")

	def test_after_the_wait_every_upgraded_server_is_checked_again(self) -> None:
		earlier = insert_server("test-update-earlier")
		later = insert_server("test-update-later")
		update = self.start_planned([earlier, later], canary_size=1, batch_size=1)
		update.servers[0].db_set("status", "Verified")
		update.servers[1].db_set("status", "Upgraded")
		update.db_set({"current_batch": 2, "wait_until": add_to_date(now_datetime(), minutes=-1)})

		with (
			patch(f"{MODULE}.is_job_enqueued", return_value=False),
			patch(f"{MODULE}.frappe.enqueue") as enqueue,
		):
			update.run_next_step()

		update.reload()
		self.assertEqual([row.status for row in update.servers], ["Checking", "Checking"])
		self.assertEqual({call.args[0].__name__ for call in enqueue.call_args_list}, {"read_server_versions"})

	def test_the_check_fails_a_server_that_rolled_back_and_verifies_a_healthy_one(self) -> None:
		rolled_back = insert_server("test-check-rolled-back")
		healthy = insert_server("test-check-healthy")
		for server in (rolled_back, healthy):
			record_synchronization(server)
		update = self.start_planned([rolled_back, healthy], canary_size=2)

		for row, versions in zip(update.servers, (OLD, TARGET), strict=True):
			row.db_set("status", "Checking")
			with patch(f"{MODULE}.get_host_versions", return_value=versions):
				read_server_versions(update.name, row.name)

		update.reload()
		self.assertEqual([row.status for row in update.servers], ["Failed", "Verified"])
		self.assertIn("metald 1.0.0, not 2.0.0", update.servers[0].status_reason)

	def test_a_verified_last_batch_completes_the_update(self) -> None:
		update = self.start_planned([insert_server("test-update-complete")])
		update.servers[0].db_set("status", "Verified")

		update.run_next_step()

		self.assertEqual(frappe.db.get_value("Metald Update", update.name, "status"), "Completed")
