"""Virtual Machine Migration — the resumable phase row for a host-to-host move.

See spec/19-vm-migration.md. This is the controller for the doctype defined in
virtual_machine_migration.json; the phase machine that ADVANCES a row lives in
atlas/atlas/migration.py (the scheduler callback).

The row is the source of truth for an in-flight migration (spec principle 2):
which VM, source/target servers, the current phase (`status`), the addresses,
the NBD handle, hydration progress, and any error. The scheduler re-enters the
recorded `status` each tick, so the row alone is enough to resume after a crash.
"""

import frappe
from frappe.model.document import Document

# Locked once written — a migration row describes one move of one VM between two
# fixed servers; repointing any of them is a new migration, not an edit. Mirrors
# VirtualMachine.IMMUTABLE_AFTER_INSERT.
IMMUTABLE_AFTER_INSERT = (
	"virtual_machine",
	"source_server",
	"target_server",
	"release_reserved_ip",
)

# Phases past which a migration is finished. Used by the per-VM in-flight guard
# (VirtualMachine.migrate refuses a second one) and the scheduler scan.
TERMINAL_STATUSES = ("Done", "Failed")


class VirtualMachineMigration(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		completed_at: DF.Datetime | None
		error_at_status: DF.Data | None
		error_message: DF.LongText | None
		hydration_last_polled: DF.Datetime | None
		hydration_percent: DF.Int
		hydration_stall_ticks: DF.Int
		ipv6_address_new: DF.Data | None
		ipv6_address_old: DF.Data | None
		nbd_pid: DF.Int
		nbd_port: DF.Int
		release_reserved_ip: DF.Check
		source_server: DF.Link | None
		status: DF.Literal[
			"Pending",
			"ExportingSnapshot",
			"TargetPreparing",
			"InjectingIdentity",
			"Hydrating",
			"CutoverStarting",
			"Repointing",
			"Cleanup",
			"Done",
			"Failed",
		]
		started_at: DF.Datetime | None
		target_server: DF.Link
		virtual_machine: DF.Link
	# end: auto-generated types

	def before_insert(self) -> None:
		"""Denormalize the source server and the VM's current address off the VM,
		default the status, and stamp the start. The target server is operator-set.
		One in-flight migration per VM is enforced in VirtualMachine.migrate (the
		caller), not here, so a direct insert in a test still works."""
		vm = frappe.get_doc("Virtual Machine", self.virtual_machine)
		if not self.source_server:
			self.source_server = vm.server
		if not self.ipv6_address_old:
			self.ipv6_address_old = vm.ipv6_address
		if not self.status:
			self.status = "Pending"
		if not self.started_at:
			self.started_at = frappe.utils.now_datetime()

	def validate(self) -> None:
		self._validate_immutability()
		self._validate_servers_differ()

	def _validate_immutability(self) -> None:
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			if getattr(original, field) != getattr(self, field):
				frappe.throw(f"{field} is immutable after insert")

	def _validate_servers_differ(self) -> None:
		if self.source_server and self.target_server and self.source_server == self.target_server:
			frappe.throw("Source and target servers must differ")

	@property
	def is_terminal(self) -> bool:
		return self.status in TERMINAL_STATUSES

	@frappe.whitelist()
	def retry(self) -> None:
		"""Re-arm a Failed migration: clear the error and drop back to the phase the
		scheduler should re-enter. Every phase is idempotent, so the next
		`reconcile_migrations` tick re-runs the last incomplete step. We do NOT
		recompute which phase to resume — the row records it in `error_at_status`
		(written by advance_migration on failure); we just flip Failed → that phase."""
		if self.status != "Failed":
			frappe.throw(f"Only a Failed migration can be retried (status is {self.status})")
		resume = self.error_at_status or "Pending"
		self.db_set({"status": resume, "error_message": None})


def active_migration_for(virtual_machine: str) -> str | None:
	"""The name of a non-terminal migration for this VM, or None. The single-flight
	guard (VirtualMachine.migrate) and the lifecycle guards (start/stop/terminate/…)
	both call this."""
	rows = frappe.get_all(
		"Virtual Machine Migration",
		filters={"virtual_machine": virtual_machine, "status": ["not in", TERMINAL_STATUSES]},
		pluck="name",
		limit=1,
	)
	return rows[0] if rows else None
