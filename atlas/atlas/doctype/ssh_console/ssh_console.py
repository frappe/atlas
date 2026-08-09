"""The operator's ad-hoc SSH command surface — the fan-out console.

A Single doctype: pick Servers and/or Virtual Machines, type one command, hit
Execute. The command runs as root over SSH on every target and the per-target
output streams back into the results table. Mirrors press's Ansible Console; the
fan-out itself lives in `atlas.atlas.ssh_console`.

Flow:
  - `execute()` runs in the request: validates, pre-creates a `Running`
    SSH Command Log (the operator's receipt), then enqueues `_execute_console`
    on the long queue. Returns `{nonce, log}` so the client can correlate the
    realtime stream and link to the saved log.
  - `_execute_console()` runs in the worker: fans out, appending each result to
    the live log and publishing it on `ssh_console_update` (nonce-keyed, so a
    stale console form ignores a previous run's events). On completion it flips
    the log to Success/Failure and publishes a final event.

There is no command allow-list, by design: `frappe.only_for("System Manager")`
plus the immutable log is the whole guardrail (the spec's operator-only model).
"""

import frappe
from frappe.model.document import Document

from atlas.atlas import ssh_console

CONSOLE_DOCTYPE = "SSH Console"
UPDATE_EVENT = "ssh_console_update"


class SSHConsole(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from atlas.atlas.doctype.ssh_command_result.ssh_command_result import SSHCommandResult
		from atlas.atlas.doctype.ssh_console_target.ssh_console_target import SSHConsoleTarget

		command: DF.Code | None
		nonce: DF.Data | None
		results: DF.Table[SSHCommandResult]
		targets: DF.Table[SSHConsoleTarget]
		timeout_seconds: DF.Int
	# end: auto-generated types

	@frappe.whitelist()
	def execute(self) -> dict:
		"""Validate, create the audit log, and enqueue the fan-out. Returns the
		nonce (realtime correlation) and the SSH Command Log name."""
		frappe.only_for("System Manager")

		command = (self.command or "").strip()
		if not command:
			frappe.throw("Enter a command to run.")
		targets = [(row.target_doctype, row.target_name) for row in self.targets]
		if not targets:
			frappe.throw("Add at least one target.")

		timeout_seconds = self.timeout_seconds or ssh_console.DEFAULT_TIMEOUT_SECONDS

		log = frappe.get_doc(
			{
				"doctype": "SSH Command Log",
				"command": command,
				"status": "Running",
				"target_count": len(targets),
				"triggered_by": frappe.session.user,
				"started": frappe.utils.now_datetime(),
			}
		)
		log.insert(ignore_permissions=True)

		frappe.enqueue(
			"atlas.atlas.doctype.ssh_console.ssh_console._execute_console",
			queue="long",
			timeout=7200,
			enqueue_after_commit=True,
			log_name=log.name,
			targets=targets,
			command=command,
			timeout_seconds=timeout_seconds,
			nonce=self.nonce,
			user=frappe.session.user,
		)
		return {"nonce": self.nonce, "log": log.name}


def _execute_console(
	log_name: str,
	targets: list,
	command: str,
	timeout_seconds: int,
	nonce: str | None,
	user: str,
) -> None:
	"""Worker entrypoint: fan the command out, streaming each result onto the
	live log and the operator's console form, then finalize the log."""
	log = frappe.get_doc("SSH Command Log", log_name)
	target_list = [ssh_console.Target(kind=kind, name=name) for kind, name in targets]

	def on_result(result: ssh_console.CommandResult) -> None:
		_append_result(log, result)
		log.save(ignore_permissions=True)
		# nosemgrep: frappe-manual-commit -- background job: persist each streamed result so a crash mid-fan-out leaves a partial but honest log, and the realtime push reflects committed state
		frappe.db.commit()
		_publish(nonce, user, log)

	results = ssh_console.run_fan_out(
		target_list, command, on_result=on_result, timeout_seconds=timeout_seconds
	)

	log.status = "Failure" if any(r.status != ssh_console.SUCCESS for r in results) else "Success"
	log.ended = frappe.utils.now_datetime()
	log.duration_milliseconds = sum(r.duration_milliseconds for r in results)
	log.save(ignore_permissions=True)
	# nosemgrep: frappe-manual-commit -- background job: persist the final status before the last realtime push
	frappe.db.commit()
	_publish(nonce, user, log)


def _append_result(log, result: ssh_console.CommandResult) -> None:
	log.append(
		"results",
		{
			"target_doctype": result.target_kind,
			"target_name": result.target_name,
			"status": result.status,
			"exit_code": result.exit_code,
			"duration_milliseconds": result.duration_milliseconds,
			"stdout": result.stdout,
			"stderr": result.stderr,
		},
	)


def _publish(nonce: str | None, user: str, log) -> None:
	"""Push the current result set to the operator's console. Nonce-keyed and
	user-scoped: only the operator who fired this run, on a console form whose
	nonce still matches, repaints. The console always has the SAME docname (it is
	a Single), so a doc-room push would also reach a sibling who opened it; the
	user scope keeps the stream private to its owner."""
	frappe.publish_realtime(
		event=UPDATE_EVENT,
		message={
			"nonce": nonce,
			"log": log.name,
			"status": log.status,
			"results": [row.as_dict() for row in log.results],
		},
		user=user,
		doctype=CONSOLE_DOCTYPE,
		docname=CONSOLE_DOCTYPE,
	)
