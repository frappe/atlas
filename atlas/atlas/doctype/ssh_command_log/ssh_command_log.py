"""The immutable audit record of one ad-hoc SSH Console run.

One row per Execute. Created `Running` the moment the operator fires a command
(the receipt), then the background worker appends a result row per target as
each host answers and flips the status to Success/Failure on completion. The
command, who ran it, and how many targets it hit are frozen at insert; only the
streamed run-state (status, timing, results) fills in afterwards.

See `atlas.atlas.ssh_console` (the fan-out engine) and the SSH Console doctype
(the operator surface that enqueues `_execute_console`).
"""

import frappe
from frappe.model.document import Document

# Frozen at insert. The run-state fields (status, started, ended,
# duration_milliseconds, results) are written by the worker as the run streams.
IMMUTABLE_AFTER_INSERT = ("command", "triggered_by", "target_count")


class SSHCommandLog(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from atlas.atlas.doctype.ssh_command_result.ssh_command_result import SSHCommandResult

		command: DF.Code
		duration_milliseconds: DF.Int
		ended: DF.Datetime | None
		results: DF.Table[SSHCommandResult]
		started: DF.Datetime | None
		status: DF.Literal["Running", "Success", "Failure"]
		target_count: DF.Int
		triggered_by: DF.Link
	# end: auto-generated types

	def validate(self) -> None:
		self._validate_immutability()

	def _validate_immutability(self) -> None:
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			if getattr(self, field) != getattr(original, field):
				frappe.throw(f"{field} is read-only after insert")
