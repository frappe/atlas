from __future__ import annotations

import subprocess
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.model.document import Document

from atlas.atlas.core.ssh import SSHResult, SSHRunner

if TYPE_CHECKING:
	from frappe.types import DF


class SSHTask(Document):
	"""One SSH command run against a Metal Server or a Virtual Machine, with its recorded result.

	The task carries no credential. It connects with the identity of the Atlas
	host, so the caller must make the target reachable before it creates a task.
	"""

	timeout_buffer_seconds = 10
	custom_script_path = "custom_script"
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		ended_at: DF.Datetime | None
		environment: DF.JSON | None
		exit_code: DF.Int
		output: DF.Code | None
		port: DF.Int
		script: DF.Code
		script_path: DF.Data | None
		ssh_user: DF.Data
		started_at: DF.Datetime | None
		status: DF.Literal["Pending", "Running", "Success", "Failed"]
		target: DF.DynamicLink
		target_type: DF.Literal["Metal Server", "Virtual Machine"]
		timeout_seconds: DF.Int
	# end: auto-generated types

	def validate(self) -> None:
		"""Reject a task without a target or a command."""
		if not 1 <= self.port <= 65_535:
			frappe.throw(_("SSH port must be between 1 and 65535."))
		if not 1 <= self.timeout_seconds <= 3_600:
			frappe.throw(_("SSH timeout must be between 1 and 3600 seconds."))
		if not self.ssh_user:
			frappe.throw(_("SSH user is required."))
		if not self.script:
			frappe.throw(_("SSH script is required."))
		self._environment()

	def after_insert(self) -> None:
		"""Run this SSH task after Frappe records it."""
		if getattr(self.flags, "run_in_background", True):
			self._enqueue()
		else:
			self.execute()

	def _enqueue(self) -> None:
		"""Queue this SSH task for execution."""
		frappe.enqueue_doc(
			self.doctype,
			self.name,
			"_run",
			queue="long",
			timeout=self.timeout_seconds,
			job_id=f"atlas||ssh-task||{self.name}",
			deduplicate=True,
			enqueue_after_commit=True,
		)

	def _run(self) -> None:
		self.execute()

	@property
	def result(self) -> SSHResult | None:
		"""Return the completed SSH result, if this log has finished."""
		if self.status in {"Pending", "Running"}:
			return None
		return SSHResult(self.output or "", self.exit_code)

	@staticmethod
	def create_for_command(
		*,
		target_type: str,
		target: str,
		command: str,
		port: int = 22,
		ssh_user: str = "root",
		environment: Mapping[str, object] | None = None,
		timeout_seconds: int = 120,
		run_in_background: bool = True,
	) -> "SSHTask":
		"""Record a shell command and run it now or in a background job."""
		return SSHTask._create(
			target_type=target_type,
			target=target,
			script_path=SSHTask.custom_script_path,
			script=command,
			port=port,
			ssh_user=ssh_user,
			environment=environment,
			timeout_seconds=timeout_seconds,
			run_in_background=run_in_background,
		)

	@staticmethod
	def create_for_script_file(
		*,
		target_type: str,
		target: str,
		script_path: str,
		port: int = 22,
		ssh_user: str = "root",
		environment: Mapping[str, object] | None = None,
		timeout_seconds: int = 120,
		run_in_background: bool = True,
	) -> "SSHTask":
		"""Record a script file source and run it now or in a background job."""
		return SSHTask._create(
			target_type=target_type,
			target=target,
			script_path=script_path,
			script=SSHRunner.get_script_source(script_path),
			port=port,
			ssh_user=ssh_user,
			environment=environment,
			timeout_seconds=timeout_seconds,
			run_in_background=run_in_background,
		)

	@staticmethod
	def _create(
		*,
		target_type: str,
		target: str,
		script_path: str,
		script: str,
		port: int,
		ssh_user: str,
		environment: Mapping[str, object] | None,
		timeout_seconds: int,
		run_in_background: bool,
	) -> "SSHTask":
		"""Create a pending SSH task."""
		task = frappe.get_doc(
			{
				"doctype": "SSH Task",
				"target_type": target_type,
				"target": target,
				"script_path": script_path,
				"port": port,
				"ssh_user": ssh_user,
				"script": script,
				"environment": frappe.as_json(environment or {}),
				"timeout_seconds": timeout_seconds,
				"status": "Pending",
			}
		)
		task.flags.run_in_background = run_in_background
		return task.insert(ignore_permissions=True)

	def execute(self) -> SSHResult:
		"""Run this SSH task and store its completed result."""
		self._mark_running()
		try:
			target = frappe.get_doc(self.target_type, self.target)
			runner = SSHRunner(target.ssh_host, self.port, self.ssh_user, target.get_ssh_proxy_command())
			result = runner.run_command(
				self.script,
				data=self._environment(),
				timeout_seconds=self.timeout_seconds,
				on_output=self._append_output,
			)
		except (OSError, subprocess.TimeoutExpired) as error:
			result = SSHResult(output=str(error), exit_code=None)

		self._finish(result)
		return result

	@classmethod
	def mark_timed_out_tasks(cls) -> None:
		"""Mark all expired SSH tasks as failed."""
		for task in frappe.get_all("SSH Task", filters={"status": "Running"}, pluck="name"):
			frappe.get_doc("SSH Task", task).mark_timed_out()

	def mark_timed_out(self, now: datetime | None = None) -> bool:
		"""Mark this running SSH task as failed after its timeout buffer."""
		if self.status != "Running" or not self.started_at:
			return False

		now = now or frappe.utils.now_datetime()
		deadline = self.started_at + timedelta(seconds=self.timeout_seconds + self.timeout_buffer_seconds)
		if now < deadline:
			return False

		self.status = "Failed"
		self.ended_at = now
		self.output = f"{self.output or ''}\nSSH execution timed out after {self.timeout_seconds} seconds."
		self.db_set(
			{"status": self.status, "ended_at": self.ended_at, "output": self.output},
			update_modified=False,
		)
		frappe.db.commit()  # nosemgrep
		return True

	def _mark_running(self) -> None:
		self.status = "Running"
		self.started_at = frappe.utils.now_datetime()
		self.ended_at = None
		self.output = ""
		self.db_set(
			{"status": self.status, "started_at": self.started_at, "ended_at": None, "output": self.output},
			update_modified=False,
		)
		frappe.db.commit()  # nosemgrep

	def _append_output(self, output: str) -> None:
		"""Append a received SSH output chunk to this task."""
		self.output = f"{self.output or ''}{output}"
		self.db_set("output", self.output, update_modified=False)
		frappe.publish_realtime(
			"ssh_task_output_update",
			doctype=self.doctype,
			docname=self.name,
			message={"name": self.name, "output": self.output},
			user=frappe.session.user,
		)
		frappe.db.commit()  # nosemgrep

	def _finish(self, result: SSHResult) -> None:
		if frappe.db.get_value(self.doctype, self.name, "status") != "Running":
			return
		if result.output and result.output != self.output:
			self.output = f"{self.output or ''}{result.output}"
		self.exit_code = result.exit_code
		self.status = "Success" if result.is_success else "Failed"
		self.ended_at = frappe.utils.now_datetime()
		self.db_set(
			{
				"output": self.output,
				"exit_code": self.exit_code,
				"status": self.status,
				"ended_at": self.ended_at,
			},
			update_modified=False,
		)
		frappe.db.commit()  # nosemgrep

	def _environment(self) -> dict[str, object]:
		environment = frappe.parse_json(self.environment or "{}")
		if not isinstance(environment, dict) or not all(isinstance(name, str) for name in environment):
			frappe.throw(_("SSH environment must be a JSON object with string keys."))
		return environment


def mark_timed_out_ssh_tasks():
	"""Mark SSH tasks that outlive their configured timeout."""
	SSHTask.mark_timed_out_tasks()


def delete_tasks_for_target(target_type: str, target: str) -> None:
	"""Delete the SSH tasks of one target. A target cannot be deleted while they exist."""
	frappe.db.delete("SSH Task", {"target_type": target_type, "target": target})
