# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime, time_diff_in_seconds

from atlas.atlas.core.exceptions import AtlasUserError
from atlas.vm.core.vm_migration import TERMINAL_STATUSES, MigrationService

if TYPE_CHECKING:
	from atlas.operations.doctype.metal_server_drain_migration.metal_server_drain_migration import (
		MetalServerDrainMigration,
	)
	from atlas.vm.doctype.virtual_machine.virtual_machine import VirtualMachine

MAXIMUM_MIGRATION_ATTEMPTS = 3


class MetalServerDrain(Document):
	"""Move every Virtual Machine off one Metal Server."""

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

		allow_any_destination: DF.Check
		allow_disk_only_fallback: DF.Check
		amended_from: DF.Link | None
		destinations: DF.Table[MetalServerDrainDestination]
		duration_seconds: DF.Duration | None
		finished_at: DF.Datetime | None
		has_memory_snapshot: DF.Check
		migrations: DF.Table[MetalServerDrainMigration]
		progress_percent: DF.Int
		retry_failed_migrations: DF.Check
		source_metal_server: DF.Link
		started_at: DF.Datetime | None
		status: DF.Literal["Draft", "Running", "Completed", "Failed", "Cancelled"]
	# end: auto-generated types

	@property
	def allowed_destinations(self) -> set[str] | None:
		"""Return the Metal Servers that placement may choose, or None for any of them."""
		if self.allow_any_destination:
			return None
		return {row.metal_server for row in self.destinations}

	@property
	def is_waiting(self) -> bool:
		"""Return whether migrations are paused, or another migration still moves a VM off the source."""
		return bool(
			frappe.db.get_single_value("Atlas Settings", "is_vm_migration_paused")
			or frappe.db.exists(
				"Virtual Machine", {"server": self.source_metal_server, "active_migration": ("is", "set")}
			)
		)

	@property
	def failed_virtual_machines(self) -> set[str]:
		"""Return the VMs whose last migration attempt failed."""
		last_statuses = {row.virtual_machine: row.status for row in self.migrations}
		return {name for name, status in last_statuses.items() if status == "failed"}

	def validate(self) -> None:
		"""Check the destination choice."""
		destinations = [row.metal_server for row in self.destinations]
		if not self.allow_any_destination and not destinations:
			frappe.throw(_("Add a destination or allow any destination."))
		if self.source_metal_server in destinations:
			frappe.throw(_("The Source Metal Server cannot be a destination."))
		if len(destinations) != len(set(destinations)):
			frappe.throw(_("Each destination can be in the drain only once."))

	def on_submit(self) -> None:
		"""Start the drain and keep new VMs off the source."""
		self.db_set({"status": "Running", "started_at": now_datetime()})
		frappe.db.set_value("Metal Server", self.source_metal_server, "is_in_maintenance", 1)

	def on_cancel(self) -> None:
		"""Stop starting migrations. The active migration finishes."""
		self.finish("Cancelled")

	def run_next_step(self) -> None:
		"""Start the next migration when the active one has finished, or finish the drain."""
		active_row = self.migrations[-1] if self.migrations else None
		is_ready = (not active_row or self.has_finished(active_row)) and not self.is_waiting

		# Start before any write: create reads the VM state from Metal, so no drain row may be locked.
		has_started = self.start_next_migration() if is_ready else False
		self.update_progress()

		if is_ready and not has_started:
			self.finish_after_last_migration()

	def has_finished(self, row: MetalServerDrainMigration) -> bool:
		"""Return whether the migration of one row has finished. A row without a migration failed to start."""
		if not row.virtual_machine_migration:
			return True
		status = frappe.db.get_value("Virtual Machine Migration", row.virtual_machine_migration, "status")
		return status in TERMINAL_STATUSES

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
		except AtlasUserError:
			# Drop a partly created migration. The failed row keeps the drain from trying this VM again.
			frappe.db.rollback()
			frappe.log_error(f"Metal Server Drain {self.name} could not migrate {virtual_machine_name}")
			row["status"] = "failed"

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
		"""Return the VMs on the source that can start a migration, oldest first."""
		return frappe.get_all(
			"Virtual Machine",
			filters={
				"server": self.source_metal_server,
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
		"""Complete the drain when the source is empty. A VM that stays on the source fails it."""
		# A cancel from Desk can commit during this step. The row lock reads its result.
		if frappe.db.get_value(self.doctype, self.name, "status", for_update=True) != "Running":
			return

		has_virtual_machines = frappe.db.exists(
			"Virtual Machine", {"server": self.source_metal_server, "is_terminating": 0}
		)
		self.finish("Failed" if has_virtual_machines else "Completed")

	def finish(self, status: str) -> None:
		"""Record the final status and the run time."""
		finished_at = now_datetime()
		duration_seconds = int(time_diff_in_seconds(finished_at, self.started_at)) if self.started_at else 0
		self.db_set(
			{"status": status, "finished_at": finished_at, "duration_seconds": duration_seconds},
			update_modified=False,
		)


def run_metal_server_drain_steps() -> None:
	"""Run one step of each running Metal Server Drain."""
	for name in frappe.get_all("Metal Server Drain", filters={"status": "Running"}, pluck="name"):
		try:
			frappe.get_doc("Metal Server Drain", name).run_next_step()
			frappe.db.commit()  # nosemgrep
		except Exception:
			frappe.db.rollback()
			frappe.log_error(f"Metal Server Drain {name} step failed")
