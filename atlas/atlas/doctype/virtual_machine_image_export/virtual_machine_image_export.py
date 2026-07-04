"""Virtual Machine Image Export — the resumable phase row for a host-to-host copy
of a base image.

See spec/08-images.md § "Two origins for a base image" and spec/24-vm-migration.md
§5.1. A local (snapshot-promoted) base image has no rootfs URL, so `sync-image`
cannot place it on another server — it lives only on the host it was promoted on.
This row drives shipping that base LV to a target the same way a VM migration ships
a VM's local base as a sub-step: NBD export of the read-only base LV + a tar of the
image directory (the kernel), the target hydrates via dm-clone, collapses to a
read-only local LV, and gains an identical local `Virtual Machine Image` row.

The transport is the migration's proven one: **plain TCP, unencrypted, no SSH** —
`qemu-nbd` bound to the source's public IPv4, the target's `nbd-client` dials it
directly (spec/24 §2.1). The base LV is read-only and immutable, so this is a
one-shot cold copy with none of a live VM's cutover machinery.

The row is the source of truth for an in-flight export (spec principle 2): which
image, source/target servers, the current phase (`status`), the NBD handle, and
hydration progress. The scheduler re-enters the recorded `status` each tick, so the
row alone is enough to resume after a crash. The phase machine that ADVANCES a row
lives in atlas/atlas/export.py (the scheduler callback), exactly as migration.py
drives Virtual Machine Migration.
"""

import frappe
from frappe.model.document import Document

# Locked once written — an export row describes one copy of one image between two
# fixed servers; repointing any of them is a new export, not an edit. Mirrors
# VirtualMachineMigration.IMMUTABLE_AFTER_INSERT.
IMMUTABLE_AFTER_INSERT = (
	"image",
	"source_server",
	"target_server",
)

# Phases past which an export is finished. Used by the per-image in-flight guard
# and the scheduler scan.
TERMINAL_STATUSES = ("Done", "Failed")


class VirtualMachineImageExport(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		base_size_bytes: DF.Int
		completed_at: DF.Datetime | None
		error_at_status: DF.Data | None
		error_message: DF.LongText | None
		hydration_last_polled: DF.Datetime | None
		hydration_percent: DF.Int
		hydration_stall_ticks: DF.Int
		image: DF.Link
		nbd_pid: DF.Int
		nbd_port: DF.Int
		progress_detail: DF.SmallText | None
		progress_percent: DF.Int
		source_server: DF.Link | None
		status: DF.Literal[
			"Pending",
			"Exporting",
			"Hydrating",
			"Finalizing",
			"Registering",
			"Cleanup",
			"Done",
			"Failed",
		]
		target_server: DF.Link
	# end: auto-generated types

	def before_insert(self) -> None:
		"""Denormalize the source server off the image (where its base LV lives),
		default the status, and stamp the start. The target server is operator-set.
		One in-flight export per image+target is enforced in export.export_image (the
		caller), not here, so a direct insert in a test still works."""
		if not self.source_server:
			self.source_server = _image_home_server(self.image)
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
		"""Re-arm a Failed export: clear the error and drop back to the phase the
		scheduler should re-enter. Every phase is idempotent, so the next
		`reconcile_image_exports` tick re-runs the last incomplete step. The phase to
		resume is recorded in `error_at_status` (written by advance_export on
		failure); we just flip Failed → that phase."""
		if self.status != "Failed":
			frappe.throw(f"Only a Failed export can be retried (status is {self.status})")
		resume = self.error_at_status or "Pending"
		self.db_set({"status": resume, "error_message": None})


def _image_home_server(image: str) -> str | None:
	"""The server a local image's base LV lives on. A local image is promoted from a
	snapshot on exactly one host; we find it by the last successful promote/build Task
	that produced the base LV there. Falls back to None so export.preflight_checks can
	raise a clear "not resolvable" error rather than the row silently mis-denormalizing.

	A promoted image records nothing pointing at its home server on the row itself
	(the LV name is derived, not stored), so the Task history is the authoritative
	trail: the promote ran on the server that holds the LV."""
	rows = frappe.db.sql(
		"""
		SELECT server FROM `tabTask`
		WHERE script IN ('promote-snapshot-image', 'promote-snapshot-image.py')
		  AND status = 'Success'
		  AND variables LIKE %(pattern)s
		ORDER BY modified DESC
		LIMIT 1
		""",
		{"pattern": f'%"IMAGE_NAME": "{image}"%'},
		as_dict=True,
	)
	return rows[0]["server"] if rows else None


def active_export_for(image: str, target_server: str) -> str | None:
	"""The name of a non-terminal export of this image to this target, or None. The
	single-flight guard (export.export_image) calls this so an operator can't stack two
	copies of the same image onto the same host."""
	rows = frappe.get_all(
		"Virtual Machine Image Export",
		filters={
			"image": image,
			"target_server": target_server,
			"status": ["not in", TERMINAL_STATUSES],
		},
		pluck="name",
		limit=1,
	)
	return rows[0] if rows else None
