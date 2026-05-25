"""SSH execution module.

`run_task` is the only entry point Atlas code uses to execute scripts on a
server. Every call produces a persisted `Task` row capturing stdout, stderr,
exit code, and timing.

The connection abstraction is a plain dict (host, ssh_private_key, user); the
`Server` wrapper lands in phase 3. We deliberately use the system `ssh` and
`scp` binaries via subprocess instead of paramiko so operators can copy-paste
the same command out of a Task row and reproduce it by hand.
"""

import json
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import frappe

if TYPE_CHECKING:
	from atlas.atlas.doctype.task.task import Task


_REPO_ROOT = Path(frappe.get_app_path("atlas", "..")).resolve()
SCRIPTS_DIRECTORY = _REPO_ROOT / "scripts"
SCRIPT_SEARCH_PATHS = [
	SCRIPTS_DIRECTORY,
	_REPO_ROOT / "atlas" / "tests" / "e2e" / "scripts",
]
KNOWN_HOSTS_PATH = Path("~/.atlas/known_hosts").expanduser()
REMOTE_STAGING_DIRECTORY = "/tmp/atlas"

SSH_OPTIONS = [
	"-o", "StrictHostKeyChecking=accept-new",
	"-o", f"UserKnownHostsFile={KNOWN_HOSTS_PATH}",
	"-o", "BatchMode=yes",
	"-o", "ConnectTimeout=30",
]


def run_task(
	connection: dict,
	script: str,
	variables: dict,
	virtual_machine: str | None = None,
	server: str | None = None,
	timeout_seconds: int = 1800,
) -> "Task":
	"""Create a Task row, execute the script over SSH, update the row.

	Raises frappe.ValidationError on any failure (SSH error, non-zero exit,
	timeout). The Task row is always saved with the outcome before the raise.
	"""
	task = frappe.get_doc({
		"doctype": "Task",
		"server": server,
		"virtual_machine": virtual_machine,
		"script": script,
		"variables": json.dumps(variables, sort_keys=True),
		"status": "Pending",
		"triggered_by": frappe.session.user if frappe.session else "Administrator",
	}).insert(ignore_permissions=True)

	_execute_into(task, connection, script, variables, timeout_seconds)
	return task


def execute_task(task_name: str) -> None:
	"""Background-job entrypoint. Runs an already-inserted Pending Task."""
	task = frappe.get_doc("Task", task_name)
	if not task.server:
		frappe.throw(f"Task {task_name} has no server; cannot resolve connection")

	server_doc = frappe.get_doc("Server", task.server)
	connection = connection_for_server(server_doc)
	variables = json.loads(task.variables or "{}")
	_execute_into(task, connection, task.script, variables, timeout_seconds=1800)


def connection_for_server(server) -> dict:
	"""Build the SSH connection dict from a Server doc."""
	from atlas.atlas.secrets import get_secret  # noqa: PLC0415

	if not server.ipv4_address:
		frappe.throw(f"Server {server.name} has no ipv4_address; cannot SSH")
	if not server.provider:
		frappe.throw(f"Server {server.name} has no provider; cannot SSH")
	private_key = get_secret("Server Provider", server.provider, "ssh_private_key")
	return {
		"host": server.ipv4_address,
		"ssh_private_key": private_key,
		"user": "root",
	}


def run_task_on_server(
	server: str,
	script: str,
	variables: dict,
	virtual_machine: str | None = None,
	timeout_seconds: int = 1800,
) -> "Task":
	"""Convenience wrapper: load the Server doc, build a connection, run_task."""
	server_doc = frappe.get_doc("Server", server)
	connection = connection_for_server(server_doc)
	return run_task(
		connection=connection,
		script=script,
		variables=variables,
		server=server,
		virtual_machine=virtual_machine,
		timeout_seconds=timeout_seconds,
	)


def wait_for_ssh(connection: dict, timeout_seconds: int = 300, poll_seconds: int = 5) -> None:
	"""Poll the host until SSH accepts a `true` command, or raise."""
	_ensure_known_hosts_directory()
	deadline = time.monotonic() + timeout_seconds
	with _ssh_key_file(connection["ssh_private_key"]) as key_path:
		while True:
			_, _, exit_code = _run_ssh(connection, key_path, "true", timeout_seconds=30)
			if exit_code == 0:
				return
			if time.monotonic() >= deadline:
				raise frappe.ValidationError(
					f"SSH to {connection['host']} not ready after {timeout_seconds}s"
				)
			time.sleep(poll_seconds)


def upload_files(connection: dict, files: list[tuple[str, str]]) -> None:
	"""scp files to the server. `files` is (local_path, remote_path) pairs.

	Not recorded as a Task. The remote parent directory is created first via
	a single SSH call so callers don't have to think about mkdir order.
	"""
	if not files:
		return

	_ensure_known_hosts_directory()
	with _ssh_key_file(connection["ssh_private_key"]) as key_path:
		remote_dirs = sorted({os.path.dirname(remote) for _, remote in files if os.path.dirname(remote)})
		if remote_dirs:
			mkdir_command = "mkdir -p " + " ".join(shlex.quote(d) for d in remote_dirs)
			_run_ssh(connection, key_path, mkdir_command, timeout_seconds=60)

		for local, remote in files:
			_run_scp(connection, key_path, local, remote, timeout_seconds=300)


def _execute_into(
	task: "Task",
	connection: dict,
	script: str,
	variables: dict,
	timeout_seconds: int,
) -> None:
	task.status = "Running"
	task.started = frappe.utils.now_datetime()
	task.save(ignore_permissions=True)
	frappe.db.commit()

	start_clock = time.monotonic()
	try:
		stdout, stderr, exit_code = _run_remote_script(
			connection, script, variables, timeout_seconds
		)
	except subprocess.TimeoutExpired:
		_finalize(
			task,
			stdout="",
			stderr=f"Timed out after {timeout_seconds}s",
			exit_code=None,
			status="Failure",
			elapsed_ms=int((time.monotonic() - start_clock) * 1000),
		)
		raise frappe.ValidationError(f"Task {task.name} timed out after {timeout_seconds}s")
	except Exception as exception:
		# scp/ssh failures during upload, missing script, etc. Mark the row
		# Failure before re-raising so it doesn't linger in Running forever.
		_finalize(
			task,
			stdout="",
			stderr=str(exception),
			exit_code=None,
			status="Failure",
			elapsed_ms=int((time.monotonic() - start_clock) * 1000),
		)
		if isinstance(exception, frappe.ValidationError):
			raise
		raise frappe.ValidationError(str(exception)) from exception

	elapsed_ms = int((time.monotonic() - start_clock) * 1000)
	status = "Success" if exit_code == 0 else "Failure"
	_finalize(task, stdout, stderr, exit_code, status, elapsed_ms)

	if status == "Failure":
		raise frappe.ValidationError(
			f"Task {task.name} ({script}) exited {exit_code}: {stderr[:500]}"
		)


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
	connection: dict,
	script: str,
	variables: dict,
	timeout_seconds: int,
) -> tuple[str, str, int]:
	from atlas.atlas.script_uploads import files_to_upload  # noqa: PLC0415

	script_path = _resolve_script(script)

	_ensure_known_hosts_directory()

	with _ssh_key_file(connection["ssh_private_key"]) as key_path:
		_run_ssh(
			connection,
			key_path,
			f"mkdir -p {shlex.quote(REMOTE_STAGING_DIRECTORY)}",
			timeout_seconds=60,
		)

		for local, remote in files_to_upload(script):
			local_path = (SCRIPTS_DIRECTORY / ".." / local).resolve()
			_run_scp(connection, key_path, str(local_path), remote, timeout_seconds=300)

		remote_script_path = f"{REMOTE_STAGING_DIRECTORY}/{script}"
		_run_scp(connection, key_path, str(script_path), remote_script_path, timeout_seconds=300)

		env_prefix = " ".join(
			f"{key}={shlex.quote(str(value))}" for key, value in variables.items()
		)
		command = f"env {env_prefix} bash -x {shlex.quote(remote_script_path)}".strip()

		return _run_ssh(connection, key_path, command, timeout_seconds=timeout_seconds)


def _run_ssh(
	connection: dict,
	key_path: str,
	remote_command: str,
	timeout_seconds: int,
) -> tuple[str, str, int]:
	user = connection.get("user", "root")
	host = connection["host"]
	args = [
		"ssh",
		"-i", key_path,
		*SSH_OPTIONS,
		f"{user}@{host}",
		remote_command,
	]
	result = subprocess.run(
		args,
		capture_output=True,
		text=True,
		timeout=timeout_seconds,
		check=False,
	)
	return result.stdout, result.stderr, result.returncode


def _run_scp(
	connection: dict,
	key_path: str,
	local_path: str,
	remote_path: str,
	timeout_seconds: int,
) -> None:
	user = connection.get("user", "root")
	host = connection["host"]
	args = [
		"scp",
		"-i", key_path,
		*SSH_OPTIONS,
		local_path,
		f"{user}@{host}:{remote_path}",
	]
	result = subprocess.run(
		args,
		capture_output=True,
		text=True,
		timeout=timeout_seconds,
		check=False,
	)
	if result.returncode != 0:
		raise frappe.ValidationError(
			f"scp {local_path} -> {remote_path} failed: {result.stderr}"
		)


class _ssh_key_file:
	"""Context manager that writes the SSH private key to a 0600 tempfile and
	deletes it on exit."""

	def __init__(self, private_key: str):
		self.private_key = private_key
		self.path: str | None = None

	def __enter__(self) -> str:
		handle = tempfile.NamedTemporaryFile(
			mode="w", delete=False, prefix="atlas-ssh-", suffix=".key"
		)
		try:
			os.chmod(handle.name, 0o600)
			key = self.private_key
			if not key.endswith("\n"):
				key += "\n"
			handle.write(key)
			handle.flush()
		finally:
			handle.close()
		self.path = handle.name
		return handle.name

	def __exit__(self, exc_type, exc, tb) -> None:
		if self.path and os.path.exists(self.path):
			try:
				os.unlink(self.path)
			except OSError:
				pass


def _resolve_script(script: str) -> Path:
	for directory in SCRIPT_SEARCH_PATHS:
		candidate = directory / script
		if candidate.is_file():
			return candidate
	raise FileNotFoundError(
		f"Script not found in any of {[str(p) for p in SCRIPT_SEARCH_PATHS]}: {script}"
	)


def _ensure_known_hosts_directory() -> None:
	parent = KNOWN_HOSTS_PATH.parent
	if not parent.exists():
		parent.mkdir(mode=0o700, parents=True, exist_ok=True)
