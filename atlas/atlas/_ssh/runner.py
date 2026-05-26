"""Task lifecycle and remote-script execution on top of `transport.py`."""

import shlex
import subprocess
import time
from typing import TYPE_CHECKING

import frappe

from atlas.atlas._ssh.transport import (
	REMOTE_STAGING_DIRECTORY,
	Connection,
	_ensure_known_hosts_directory,
	run_scp,
	run_ssh,
	ssh_key_file,
)

if TYPE_CHECKING:
	from atlas.atlas.doctype.task.task import Task


def run_task(
	*,
	script: str,
	variables: dict,
	server: str | None = None,
	connection: Connection | None = None,
	virtual_machine: str | None = None,
	timeout_seconds: int = 1800,
) -> "Task":
	"""Create a Task row, execute the script over SSH, update the row.

	Exactly one of `server` or `connection` must be provided:
	  - `server=<name>` is the production path: loads the Server doc and
	    builds the connection from it.
	  - `connection=<Connection>` is for bootstrap, where the Server row may
	    not yet have a usable provider linkage.

	Raises frappe.ValidationError on any failure (SSH error, non-zero exit,
	timeout). The Task row is always saved with the outcome before the raise.
	"""
	if (server is None) == (connection is None):
		frappe.throw("run_task: pass exactly one of server= or connection=")

	if connection is None:
		server_doc = frappe.get_doc("Server", server)
		connection = connection_for_server(server_doc)

	task = frappe.get_doc({
		"doctype": "Task",
		"server": server,
		"virtual_machine": virtual_machine,
		"script": script,
		"status": "Pending",
		"triggered_by": frappe.session.user if frappe.session else "Administrator",
	})
	task.variables_dict = variables
	task.insert(ignore_permissions=True)

	_execute_into(task, connection, script, variables, timeout_seconds)
	return task


def execute_task(task_name: str) -> None:
	"""Background-job entrypoint. Runs an already-inserted Pending Task."""
	task = frappe.get_doc("Task", task_name)
	if not task.server:
		frappe.throw(f"Task {task_name} has no server; cannot resolve connection")

	server_doc = frappe.get_doc("Server", task.server)
	connection = connection_for_server(server_doc)
	_execute_into(task, connection, task.script, task.variables_dict, timeout_seconds=1800)


def connection_for_server(server) -> Connection:
	"""Build the SSH Connection from a Server doc."""
	from atlas.atlas.secrets import get_secret

	if not server.ipv4_address:
		frappe.throw(f"Server {server.name} has no ipv4_address; cannot SSH")
	if not server.provider:
		frappe.throw(f"Server {server.name} has no provider; cannot SSH")
	private_key = get_secret("Server Provider", server.provider, "ssh_private_key")
	return Connection(host=server.ipv4_address, ssh_private_key=private_key)


def _execute_into(
	task: "Task",
	connection: Connection,
	script: str,
	variables: dict,
	timeout_seconds: int,
) -> None:
	_mark_running(task)
	start = time.monotonic()
	try:
		stdout, stderr, exit_code = _run_remote_script(
			connection, script, variables, timeout_seconds
		)
	except subprocess.TimeoutExpired as timeout:
		_finalize(task, "", f"Timed out after {timeout.timeout}s", None,
			"Failure", _elapsed_ms(start))
		frappe.throw(f"Task {task.name} timed out after {timeout.timeout}s")
	except Exception as exception:
		_finalize(task, "", str(exception), None, "Failure",
			_elapsed_ms(start))
		if isinstance(exception, frappe.ValidationError):
			raise
		raise frappe.ValidationError(str(exception)) from exception

	status = "Success" if exit_code == 0 else "Failure"
	_finalize(task, stdout, stderr, exit_code, status, _elapsed_ms(start))
	if status == "Failure":
		# Tail, not head: scripts run under `bash -x`, so the first hundreds of
		# chars are tracing noise and the real error message lives near the end.
		frappe.throw(
			f"Task {task.name} ({script}) exited {exit_code}: {stderr[-500:]}"
		)


def _mark_running(task: "Task") -> None:
	task.status = "Running"
	task.started = frappe.utils.now_datetime()
	task.save(ignore_permissions=True)
	frappe.db.commit()


def _elapsed_ms(start: float) -> int:
	return int((time.monotonic() - start) * 1000)


def _finalize(
	task: "Task",
	stdout: str,
	stderr: str,
	exit_code: int | None,
	status: str,
	elapsed_ms: int,
) -> None:
	task.stdout = stdout
	task.stderr = stderr
	task.exit_code = exit_code
	task.status = status
	task.ended = frappe.utils.now_datetime()
	task.duration_milliseconds = elapsed_ms
	task.save(ignore_permissions=True)
	frappe.db.commit()


def _run_remote_script(
	connection: Connection,
	script: str,
	variables: dict,
	timeout_seconds: int,
) -> tuple[str, str, int]:
	from atlas.atlas import scripts_catalog
	from atlas.atlas.script_uploads import files_to_upload

	script_path = scripts_catalog.resolve(script)

	_ensure_known_hosts_directory()

	with ssh_key_file(connection.ssh_private_key) as key_path:
		run_ssh(
			connection,
			key_path,
			f"mkdir -p {shlex.quote(REMOTE_STAGING_DIRECTORY)}",
			timeout_seconds=60,
		)

		for local, remote in files_to_upload(script):
			local_path = (scripts_catalog.scripts_directory() / ".." / local).resolve()
			run_scp(connection, key_path, str(local_path), remote, timeout_seconds=300)

		remote_script_path = f"{REMOTE_STAGING_DIRECTORY}/{script}"
		run_scp(connection, key_path, str(script_path), remote_script_path, timeout_seconds=300)

		env_prefix = " ".join(
			f"{key}={shlex.quote(str(value))}" for key, value in variables.items()
		)
		command = f"env {env_prefix} bash -x {shlex.quote(remote_script_path)}".strip()

		return run_ssh(connection, key_path, command, timeout_seconds=timeout_seconds)
