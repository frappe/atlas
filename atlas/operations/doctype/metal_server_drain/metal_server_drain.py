# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

from collections import Counter
from functools import cached_property
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime, time_diff_in_seconds

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.vm.core.vm_migration import TERMINAL_STATUSES, MigrationService, get_active_migrations

if TYPE_CHECKING:
	from atlas.vm.core.sleepy_rebalance import SleepyHostRebalance
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine
	from atlas.vm.doctype.virtual_machine_migration.virtual_machine_migration import (
		VirtualMachineMigration,
	)

MAXIMUM_MIGRATION_ATTEMPTS = 3


class MetalServerDrain(Document):
	"""Move Virtual Machines off one Metal Server."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from atlas.operations.doctype.metal_server_drain_destination.metal_server_drain_destination import (
			MetalServerDrainDestination,
		)
		from atlas.operations.doctype.metal_server_drain_migration.metal_server_drain_migration import (
			MetalServerDrainMigration,
		)
		from atlas.operations.doctype.metal_server_drain_virtual_machine.metal_server_drain_virtual_machine import (
			MetalServerDrainVirtualMachine,
		)

		allow_any_destination: DF.Check
		allow_disk_only_fallback: DF.Check
		amended_from: DF.Link | None
		destinations: DF.Table[MetalServerDrainDestination]
		duration_seconds: DF.Duration | None
		error_message: DF.SmallText | None
		finished_at: DF.Datetime | None
		has_memory_snapshot: DF.Check
		is_sleepy_rebalance: DF.Check
		migrations: DF.Table[MetalServerDrainMigration]
		move_only_selected_vms: DF.Check
		parallel_migrations: DF.Int
		progress_percent: DF.Int
		retry_failed_migrations: DF.Check
		source_metal_server: DF.Link
		started_at: DF.Datetime | None
		status: DF.Literal["Draft", "Running", "Completed", "Failed", "Cancelled"]
		virtual_machines: DF.Table[MetalServerDrainVirtualMachine]
	# end: auto-generated types

	@property
	def allowed_destinations(self) -> set[str] | None:
		"""Return the Metal Servers that placement may choose, or None for any of them."""
		if self.allow_any_destination:
			return None
		return {row.metal_server for row in self.destinations}

	@cached_property
	def rebalance_policy(self) -> SleepyHostRebalance:
		"""Keep one eligibility snapshot for this drain step."""
		from atlas.vm.core.sleepy_rebalance import SleepyHostRebalance

		return SleepyHostRebalance(
			self.source_metal_server, {row.virtual_machine for row in self.virtual_machines}
		)

	@property
	def virtual_machine_filters(self) -> dict[str, str | tuple[str, list[str]]]:
		"""Select all or only the chosen VMs on the source."""
		filters: dict[str, str | tuple[str, list[str]]] = {"server": self.source_metal_server}
		if self.move_only_selected_vms:
			filters["name"] = ("in", [row.virtual_machine for row in self.virtual_machines])
		return filters

	@property
	def active_migration_count(self) -> int:
		"""Return the number of migrations of this drain that have not finished."""
		return frappe.db.count(
			"Virtual Machine Migration",
			{"metal_server_drain": self.name, "status": ("not in", list(TERMINAL_STATUSES))},
		)

	@property
	def failed_virtual_machines(self) -> set[str]:
		"""Return the VMs whose last migration attempt failed."""
		last_statuses = {row.virtual_machine: row.status for row in self.migrations}
		return {name for name, status in last_statuses.items() if status == "failed"}

	def validate(self) -> None:
		"""Check the VM selection, destinations, and number of parallel migrations."""
		self.validate_virtual_machines()
		destinations = [row.metal_server for row in self.destinations]
		if not self.allow_any_destination and not destinations:
			frappe.throw(_("Add a destination or allow any destination."))
		if self.source_metal_server in destinations:
			frappe.throw(_("The Source Metal Server cannot be a destination."))
		if len(destinations) != len(set(destinations)):
			frappe.throw(_("Each destination can be in the drain only once."))
		if self.parallel_migrations < 1:
			frappe.throw(_("Parallel Migrations must be at least 1."))

	def validate_virtual_machines(self) -> None:
		"""Require distinct, eligible VMs from the source when selection is enabled."""
		if not self.move_only_selected_vms:
			return
		virtual_machines = [row.virtual_machine for row in self.virtual_machines]
		if not virtual_machines:
			frappe.throw(_("Add at least one Virtual Machine."))
		if len(virtual_machines) != len(set(virtual_machines)):
			frappe.throw(_("Each Virtual Machine can be in the drain only once."))
		if frappe.db.count(
			"Virtual Machine", {**self.virtual_machine_filters, "is_draft": 0, "is_terminating": 0}
		) != len(virtual_machines):
			frappe.throw(
				_("Select Virtual Machines on the Source Metal Server that are not drafts or terminating.")
			)

	def before_submit(self) -> None:
		"""Allow one running drain per source server."""
		# The source row lock orders two submits for one server. The locking read sees the
		# drain that the first submit committed, which a plain read could miss.
		source_title = frappe.db.get_value("Metal Server", self.source_metal_server, "title", for_update=True)
		running_drain = frappe.db.get_value(
			"Metal Server Drain",
			{"source_metal_server": self.source_metal_server, "status": "Running", "name": ("!=", self.name)},
			"name",
			for_update=True,
		)
		if running_drain:
			frappe.throw(
				_("Metal Server {0} already has running drain {1}.").format(source_title, running_drain)
			)

	def on_submit(self) -> None:
		"""Start the drain and put manual drain sources in maintenance."""
		self.db_set({"status": "Running", "started_at": now_datetime()})
		if not self.is_sleepy_rebalance:
			frappe.db.set_value("Metal Server", self.source_metal_server, "is_in_maintenance", 1)

	def on_cancel(self) -> None:
		"""Stop starting migrations. The active migration finishes."""
		self.finish("Cancelled")

	def run_next_step(self) -> None:
		"""Start migrations until Parallel Migrations run, or finish the drain when none is left."""
		is_paused = frappe.db.get_single_value("Atlas Settings", "is_vm_migration_paused")
		has_started = False
		while not is_paused and self.active_migration_count < self.parallel_migrations:
			if not self.start_next_migration():
				break
			has_started = True
			# The next create reads the VM state from Metal, so this start must hold no lock then.
			frappe.db.commit()  # nosemgrep
			# A failed start ends the step, so a retry of that VM waits at least one step.
			if not self.migrations[-1].virtual_machine_migration:
				break

		if not (is_paused or has_started or self.active_migration_count):
			self.finish_after_last_migration()
		self.update_progress()

	def start_next_migration(self) -> bool:
		"""Start a migration for the next VM on the source. Return False when no VM is left to try."""
		virtual_machine_name = self.get_next_virtual_machine()
		if not virtual_machine_name:
			return False

		virtual_machine: VirtualMachine = frappe.get_doc("Virtual Machine", virtual_machine_name)
		row = {"virtual_machine": virtual_machine_name, "docstatus": self.docstatus}
		try:
			row["virtual_machine_migration"] = MigrationService.create(
				virtual_machine,
				has_memory_snapshot=bool(self.has_memory_snapshot),
				allow_disk_only_fallback=bool(self.allow_disk_only_fallback),
				metal_server_drain=self.name,
			)
			row["status"] = "scheduled"
		except AtlasUserError as error:
			# Drop a partly created migration. The failed row keeps the drain from trying this VM again.
			frappe.db.rollback()
			frappe.log_error(f"Metal Server Drain {self.name} could not migrate {virtual_machine_name}")
			row["status"] = "failed"
			row["error_message"] = str(error)

		self.append("migrations", row).db_insert()
		return True

	def get_next_virtual_machine(self) -> str | None:
		"""Return a VM without an attempt, else the failed VM with the fewest attempts when retries are on."""
		candidates = self.get_candidate_virtual_machines()
		attempts = Counter(row.virtual_machine for row in self.migrations)
		untried = [name for name in candidates if name not in attempts]
		if untried:
			return untried[0]
		if not self.retry_failed_migrations:
			return None

		failed = self.failed_virtual_machines
		retryable = [
			name for name in candidates if name in failed and attempts[name] < MAXIMUM_MIGRATION_ATTEMPTS
		]
		return min(retryable, key=lambda name: attempts[name], default=None)

	def get_candidate_virtual_machines(self) -> list[str]:
		"""Return eligible VMs in automatic policy order, or oldest first for manual drains."""
		if self.is_sleepy_rebalance:
			return [machine.name for machine in self.rebalance_policy.candidates]
		return frappe.get_all(
			"Virtual Machine",
			filters={
				**self.virtual_machine_filters,
				"is_draft": 0,
				"is_terminating": 0,
				"active_migration": ("is", "not set"),
			},
			order_by="creation asc",
			pluck="name",
		)

	def update_progress(self) -> None:
		"""Store the share of VMs that this drain moved off the source."""
		virtual_machines = {row.virtual_machine for row in self.migrations}
		virtual_machines.update(self.get_candidate_virtual_machines())
		moved = {row.virtual_machine for row in self.migrations if row.status == "completed"}
		progress_percent = 100 * len(moved) // len(virtual_machines) if virtual_machines else 100
		if progress_percent != self.progress_percent:
			self.db_set("progress_percent", progress_percent, update_modified=False)

	def finish_after_last_migration(self) -> None:
		"""Complete the drain when no VM in its scope is left on the source."""
		eligible = set(self.get_candidate_virtual_machines()) if self.is_sleepy_rebalance else None
		# A cancel from Desk can commit during this step. The row lock reads its result.
		if frappe.db.get_value(self.doctype, self.name, "status", for_update=True) != "Running":
			return
		# A migration that this drain did not start can still move a VM off the source.
		if not self.move_only_selected_vms and get_active_migrations(self.source_metal_server):
			return

		self.refresh_migration_rows()
		remaining = frappe.get_all(
			"Virtual Machine",
			filters={**self.virtual_machine_filters, "is_terminating": 0},
			order_by="creation asc",
			fields=["name", "active_migration"],
		)
		if any(virtual_machine.active_migration for virtual_machine in remaining):
			return
		if eligible is not None:
			remaining = [virtual_machine for virtual_machine in remaining if virtual_machine.name in eligible]
		if remaining:
			self.finish(
				"Failed", self.get_failure_message([virtual_machine.name for virtual_machine in remaining])
			)
		else:
			self.finish("Completed")

	def get_failure_message(self, virtual_machines: list[str]) -> str:
		"""Explain, for each VM still on the source, why its attempts failed and how often."""
		reasons: dict[str, list[str]] = {}
		for row in self.migrations:
			reason = row.error_message or _("The migration ended as {0} without an error.").format(row.status)
			reasons.setdefault(row.virtual_machine, []).append(reason)

		lines = [_("Virtual machines still on the source: {0}.").format(len(virtual_machines))]
		for name in virtual_machines:
			attempts = reasons.get(name)
			if not attempts:
				lines += ["", f"{name}: " + _("No migration was started.")]
				continue

			count = _("1 attempt") if len(attempts) == 1 else _("{0} attempts").format(len(attempts))
			lines += ["", f"{name}, {count}:"]
			lines += [f"- {reason} (x{count})" for reason, count in Counter(attempts).items()]
		return "\n".join(lines)

	def refresh_migration_rows(self) -> None:
		"""Copy each migration into its row again, in case a migration change missed its row."""
		for row in self.migrations:
			if row.virtual_machine_migration:
				migration: VirtualMachineMigration = frappe.get_doc(
					"Virtual Machine Migration", row.virtual_machine_migration
				)
				row.db_set(migration.get_drain_row_values(), update_modified=False)

	def finish(self, status: str, error_message: str | None = None) -> None:
		"""Record the final status, the run time, and why the drain failed."""
		finished_at = now_datetime()
		duration_seconds = int(time_diff_in_seconds(finished_at, self.started_at)) if self.started_at else 0
		self.db_set(
			{
				"status": status,
				"finished_at": finished_at,
				"duration_seconds": duration_seconds,
				"error_message": error_message,
			},
			update_modified=False,
		)

	@classmethod
	def start(
		cls,
		source_metal_server: str,
		*,
		destinations: list[str] | None = None,
		has_memory_snapshot: bool = False,
		allow_disk_only_fallback: bool = True,
		retry_failed_migrations: bool = True,
		parallel_migrations: int = 1,
	) -> MetalServerDrain:
		"""Create and submit a drain of one Metal Server. No destinations means any destination."""
		drain = frappe.get_doc(
			{
				"doctype": "Metal Server Drain",
				"source_metal_server": source_metal_server,
				"allow_any_destination": int(not destinations),
				"destinations": [{"metal_server": destination} for destination in destinations or []],
				"has_memory_snapshot": int(has_memory_snapshot),
				"allow_disk_only_fallback": int(allow_disk_only_fallback),
				"retry_failed_migrations": int(retry_failed_migrations),
				"parallel_migrations": parallel_migrations,
			}
		)
		drain.insert()
		drain.submit()
		return drain


def run_metal_server_drain_steps() -> None:
	"""Run one step of each running Metal Server Drain."""
	for name in frappe.get_all("Metal Server Drain", filters={"status": "Running"}, pluck="name"):
		try:
			frappe.get_doc("Metal Server Drain", name).run_next_step()
			frappe.db.commit()  # nosemgrep
		except Exception:
			frappe.db.rollback()
			frappe.log_error(f"Metal Server Drain {name} step failed")
