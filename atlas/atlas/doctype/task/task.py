import json
from typing import ClassVar

import frappe
from frappe import _
from frappe.model.document import Document

IMMUTABLE_AFTER_INSERT = ("server", "virtual_machine", "script", "variables", "triggered_by")

SCRIPT_LABELS = {
	# Task.script is a VERB (no .py/.sh suffix): `provision-vm`, executed on the
	# host as `atlas provision-vm`. The on-disk file keeps its extension; only the
	# Task identifier drops it. See scripts_catalog (the verb authority).
	#
	# Verb + Noun when the verb creates a *new* object.
	"bootstrap-server": "Bootstrap Server",
	"sync-image": "Sync Image",
	"provision-vm": "Create Virtual Machine",
	"snapshot-vm": "Snapshot Virtual Machine",
	"warm-snapshot-vm": "Capture Warm Snapshot",
	# Verb-only when the verb operates on the *same* object.
	# reboot-server stays a shell verb (reboot-server.sh; two lines).
	"reboot-server": "Reboot",
	"start-vm": "Start",
	"stop-vm": "Stop",
	"snapshot-stop-vm": "Stop",
	"restart-vm": "Restart",
	"pause-vm": "Pause",
	"resume-vm": "Resume",
	"rebuild-vm": "Rebuild",
	"resize-vm": "Resize",
	"terminate-vm": "Terminate",
	"delete-snapshot-vm": "Delete Snapshot",
	# Networking — one verb drives reserved-IP attach and detach, so a
	# neutral noun reads correctly in both directions.
	"vm-reserved-ip": "Update Reserved IP",
	# TLS.
	"issue-cert": "Issue Certificate",
	# Guest / recipe-side synthetic script names (already suffix-less; run
	# in-guest over guest-SSH and recorded for the audit trail — see proxy.py,
	# image_build.py, image_recipes.py, deploy_site.py).
	"bench-build": "Build Bench",
	"bench-warm": "Warm Bench",
	"deploy-site": "Deploy Site",
	"proxy-build": "Build Proxy",
	"proxy-sync": "Sync Proxy",
	"proxy-push-cert": "Push Certificate",
}

# Scripts a Failure-state Task is allowed to retry from the form button.
# Server scripts re-run through Server.run_task_dialog; VM lifecycle scripts
# re-run through the VM's matching controller method so state-machine guards
# stay live.
RETRYABLE_VM_SCRIPTS: ClassVar = {
	"provision-vm": "provision",
	"start-vm": "start",
	"stop-vm": "stop",
	"snapshot-stop-vm": "stop",
	"restart-vm": "restart",
	"terminate-vm": "terminate",
}
RETRYABLE_SERVER_SCRIPTS = frozenset({"bootstrap-server", "reboot-server", "sync-image"})


class Task(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		duration_milliseconds: DF.Int
		ended: DF.Datetime | None
		exit_code: DF.Int
		live_output: DF.Code | None
		progress_line: DF.Data | None
		script: DF.Data
		server: DF.Link | None
		started: DF.Datetime | None
		status: DF.Literal["Pending", "Running", "Success", "Failure"]
		stderr: DF.Code | None
		stdout: DF.Code | None
		subject: DF.Data | None
		triggered_by: DF.Link
		variables: DF.LongText
		virtual_machine: DF.Link | None
	# end: auto-generated types

	@property
	def variables_dict(self) -> dict:
		return json.loads(self.variables or "{}")

	@variables_dict.setter
	def variables_dict(self, value: dict) -> None:
		if not isinstance(value, dict):
			frappe.throw(_("Task.variables_dict must be a dict"))
		self.variables = json.dumps(value, sort_keys=True)

	def before_insert(self) -> None:
		if not self.subject:
			self.subject = self._build_subject()

	def validate(self) -> None:
		if not self.variables:
			frappe.throw(_("variables is required"))
		self._validate_variables_json()
		self._validate_immutability()

	def after_insert(self) -> None:
		self._publish_update()

	def on_update(self) -> None:
		self._publish_update()
		self._propagate_status_to_virtual_machine()

	@frappe.whitelist()
	def retry(self) -> str:
		"""Re-run the failed Task. Returns the new Task's name."""
		if self.status != "Failure":
			frappe.throw(f"Only failed Tasks can be retried (this one is {self.status}).")

		if self.script in RETRYABLE_VM_SCRIPTS:
			if not self.virtual_machine:
				frappe.throw(f"Cannot retry {self.script}: this Task has no Virtual Machine.")
			method_name = RETRYABLE_VM_SCRIPTS[self.script]
			virtual_machine = frappe.get_doc("Virtual Machine", self.virtual_machine)
			result = getattr(virtual_machine, method_name)()
			return result if isinstance(result, str) else result.get("start_task") or result.get("name")

		if self.script in RETRYABLE_SERVER_SCRIPTS:
			if not self.server:
				frappe.throw(f"Cannot retry {self.script}: this Task has no Server.")
			server = frappe.get_doc("Server", self.server)
			return server.run_task_dialog(script=self.script, variables=self.variables_dict)

		frappe.throw(f"Script {self.script} is not retriable from the Task form.")

	def _build_subject(self) -> str:
		"""Subject is the verb (or verb-noun) label for the script.
		Target identity (Server / VM) lives in dedicated columns and
		dashboard chips — duplicating it in the subject was noise."""
		return SCRIPT_LABELS.get(self.script, self.script or "Task")

	def _validate_variables_json(self) -> None:
		try:
			parsed = json.loads(self.variables)
		except json.JSONDecodeError as exception:
			frappe.throw(f"variables must be valid JSON: {exception}")
		if not isinstance(parsed, dict):
			frappe.throw(_("variables must be a JSON object"))

	def _validate_immutability(self) -> None:
		if self.is_new():
			return
		original = self.get_doc_before_save()
		if not original:
			return
		for field in IMMUTABLE_AFTER_INSERT:
			if getattr(self, field) != getattr(original, field):
				frappe.throw(f"{field} is read-only after insert")

	def _propagate_status_to_virtual_machine(self) -> None:
		"""Flip VM status to Failed when its provision Task ends in Failure.

		The VM controller's `provision()` only saves `Running` *after* run_task
		returns. On a raised exception the row is unchanged (still Pending).
		Without this hook, an operator looking at the VM form sees Pending and
		has no clue the last provision attempt blew up.
		"""
		if self.status != "Failure" or self.script != "provision-vm" or not self.virtual_machine:
			return
		current = frappe.db.get_value("Virtual Machine", self.virtual_machine, "status")
		if current not in ("Pending", "Running"):
			return
		frappe.db.set_value("Virtual Machine", self.virtual_machine, "status", "Failed")
		frappe.publish_realtime(
			event="virtual_machine_update",
			message={"name": self.virtual_machine, "status": "Failed"},
			doctype="Virtual Machine",
			docname=self.virtual_machine,
		)

	def publish_log(self, live_output: str, progress_line: str) -> None:
		"""Push the current streamed log buffer to the operators' realtime room.

		The lighter sibling of `_publish_update`: lifecycle transitions
		(`Pending`→`Running`→…) ride `task_update` and repaint the status pill;
		the high-frequency between-state log updates ride this `task_log` event so
		we don't re-serialize the full status payload on every poll. The Task form
		REPLACES the Live Output panel with `live_output` and shows `progress_line`
		as the one-line "what's happening now".

		We send the whole (bounded) buffer, not a delta, because the server's
		`live_output` is already a fixed last-N-KB window it overwrites each poll —
		sending deltas would let a client that joined late, dropped an event, or
		reloaded drift out of sync with that window. A full replace is always
		consistent and the buffer is bounded (LIVE_OUTPUT_BUFFER_BYTES), so the
		payload stays small. Doc-scoped room, same as `_publish_update`, so only
		operators viewing this Task are notified."""
		frappe.publish_realtime(
			event="task_log",
			message={"name": self.name, "live_output": live_output, "progress_line": progress_line},
			doctype="Task",
			docname=self.name,
		)

	def _publish_update(self) -> None:
		payload = {
			"name": self.name,
			"status": self.status,
			"exit_code": self.exit_code,
			"duration_milliseconds": self.duration_milliseconds,
			"server": self.server,
			"virtual_machine": self.virtual_machine,
			"subject": self.subject,
		}
		# Document-scoped room so other operators viewing other Tasks aren't
		# spammed. The Task form subscribes with the same event name.
		frappe.publish_realtime(
			event="task_update",
			message=payload,
			doctype="Task",
			docname=self.name,
		)
