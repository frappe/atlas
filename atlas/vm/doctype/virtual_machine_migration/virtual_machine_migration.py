from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.core.exceptions import AtlasUserError


class VirtualMachineMigration(Document):
	"""One move of a virtual machine between Metal Servers."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from atlas.vm.doctype.virtual_machine_migration_transfer.virtual_machine_migration_transfer import (
			VirtualMachineMigrationTransfer,
		)

		destination_metal_server: DF.Link | None
		destination_metal_server_selection_attempts: DF.Int
		destination_metal_server_selection_message: DF.SmallText | None
		duration_seconds: DF.Duration | None
		error_at: DF.Datetime | None
		error_code: DF.Data | None
		error_message: DF.SmallText | None
		finished_at: DF.Datetime | None
		last_destination_metal_server_selection_at: DF.Datetime | None
		progress_percent: DF.Int
		scheduled_at: DF.Datetime | None
		source_metal_server: DF.Link
		started_at: DF.Datetime | None
		status: DF.Literal[
			"scheduled",
			"preparing",
			"copying",
			"cutting_over",
			"starting",
			"finalizing",
			"canceling",
			"completed",
			"failed",
			"aborted",
		]
		target_cpu_millicores: DF.Int
		target_disk_mib: DF.Int
		target_memory_mib: DF.Int
		transfers: DF.Table[VirtualMachineMigrationTransfer]
		virtual_machine: DF.Link
	# end: auto-generated types

	def after_insert(self) -> None:
		"""Claim the VM and queue destination selection after this transaction commits."""
		from atlas.vm.core.vm_migration import MigrationService, reconcile_migration

		MigrationService.schedule(self)
		reconcile_migration(self.name)

	def before_save(self) -> None:
		"""Allow only the migration worker to change an existing record."""
		if not self.is_new() and not getattr(self.flags, "updated_by_vm_migration_worker", False):
			frappe.throw(_("Atlas updates migration records."), exc=AtlasUserError)

	@frappe.whitelist(methods=["POST"])
	def abort(self) -> None:
		"""Request cancellation and queue the migration worker."""
		if self.status in {"completed", "failed", "aborted"}:
			frappe.throw(_("This migration is already finished."), exc=AtlasUserError)

		frappe.get_doc("Virtual Machine", self.virtual_machine).check_permission("write")
		from atlas.vm.core.vm_migration import MigrationService, reconcile_migration

		MigrationService(self).request_abort()
		reconcile_migration(self.name)


def on_doctype_update() -> None:
	"""Index destination reservations for VM placement."""
	frappe.db.add_index(
		"Virtual Machine Migration",
		["destination_metal_server", "status"],
		"destination_metal_server_status",
	)
