import json
from typing import ClassVar

import frappe
from frappe.model.document import Document

IMMUTABLE_AFTER_INSERT = ("server", "virtual_machine", "script", "variables", "triggered_by")

SCRIPT_LABELS = {
	# Verb + Noun when the script creates a *new* object.
	"bootstrap-server.sh": "Bootstrap Server",
	"sync-image.sh": "Sync Image",
	"provision-vm.sh": "Create Virtual Machine",
	# Verb-only when the script operates on the *same* object.
	"reboot-server.sh": "Reboot",
	"start-vm.sh": "Start",
	"stop-vm.sh": "Stop",
	"restart-vm.sh": "Restart",
	"terminate-vm.sh": "Terminate",
}

# Scripts a Failure-state Task is allowed to retry from the form button.
# Server scripts re-run through Server.run_task_dialog; VM lifecycle scripts
# re-run through the VM's matching controller method so state-machine guards
# stay live.
RETRYABLE_VM_SCRIPTS: ClassVar = {
	"provision-vm.sh": "provision",
	"start-vm.sh": "start",
	"stop-vm.sh": "stop",
	"restart-vm.sh": "restart",
	"terminate-vm.sh": "terminate",
}
RETRYABLE_SERVER_SCRIPTS = frozenset({"bootstrap-server.sh", "reboot-server.sh", "sync-image.sh"})


class Task(Document):
	@property
	def variables_dict(self) -> dict:
		return json.loads(self.variables or "{}")

	@variables_dict.setter
	def variables_dict(self, value: dict) -> None:
		if not isinstance(value, dict):
			frappe.throw("Task.variables_dict must be a dict")
		self.variables = json.dumps(value, sort_keys=True)

	def before_insert(self) -> None:
		if not self.subject:
			self.subject = self._build_subject()

	def validate(self) -> None:
		if not self.variables:
			frappe.throw("variables is required")
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
			frappe.throw("variables must be a JSON object")

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
		if (
			self.status != "Failure"
			or self.script != "provision-vm.sh"
			or not self.virtual_machine
		):
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
