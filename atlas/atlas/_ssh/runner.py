"""Task lifecycle and remote-script execution on top of `transport.py`."""

import os
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
from atlas.atlas.providers.fake_tasks import is_fake_server

if TYPE_CHECKING:
	from atlas.atlas.doctype.task.task import Task

# Where `Server.bootstrap()` durably places the shared `atlas` package
# (`…/bin/atlas/*.py`). Python tasks reach it via PYTHONPATH instead of a per-Task
# re-upload — see `_remote_command` and script_uploads.py. The dir on the path is
# the package's PARENT so `import atlas` resolves the `atlas/` directory under it.
DURABLE_PACKAGE_DIRECTORY = "/var/lib/atlas/bin"

# Where the pre-cutover runner staged the package per Task. The entry points'
# `sys.path.insert(0, <staging>/lib)` shim puts this AHEAD of PYTHONPATH, so a
# leftover copy on a legacy host shadows the durable package with stale modules.
# _run_remote_script purges it before every Task.
STALE_STAGED_PACKAGE_DIRECTORY = f"{REMOTE_STAGING_DIRECTORY}/lib"


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

	# Fake provider (developer_mode): a Task on a Fake-backed Server succeeds (or
	# fails on demand) with no SSH. Only the server= path can be fake — the
	# connection= path is bootstrap's pre-row escape hatch, never a Fake host.
	if server is not None and is_fake_server(server):
		from atlas.atlas.providers.fake_tasks import run_fake_task

		return run_fake_task(server, script, variables, virtual_machine)

	if connection is None:
		server_doc = frappe.get_doc("Server", server)
		connection = connection_for_server(server_doc)

	task = frappe.get_doc(
		{
			"doctype": "Task",
			"server": server,
			"virtual_machine": virtual_machine,
			"script": script,
			"status": "Pending",
			"triggered_by": frappe.session.user if frappe.session else "Administrator",
		}
	)
	task.variables_dict = variables
	task.insert(ignore_permissions=True)

	_execute_into(task, connection, script, variables, timeout_seconds)
	return task


def execute_task(task_name: str) -> None:
	"""Background-job entrypoint. Runs an already-inserted Pending Task."""
	task = frappe.get_doc("Task", task_name)
	if not task.server:
		frappe.throw(f"Task {task_name} has no server; cannot resolve connection")

	# Fake server: finalize the existing row in place, no SSH. _fake_task_outcome
	# reuses the same synthesis run_task uses, so a pre-inserted Task ends up
	# identical to one created on the synchronous fake path.
	if is_fake_server(task.server):
		from atlas.atlas.providers.fake_tasks import finalize_fake_task

		finalize_fake_task(task)
		return

	server_doc = frappe.get_doc("Server", task.server)
	connection = connection_for_server(server_doc)
	_execute_into(task, connection, task.script, task.variables_dict, timeout_seconds=1800)


def connection_for_server(server) -> Connection:
	"""Build the SSH Connection from a Server doc."""
	import atlas
	from atlas.atlas.secrets import get_ssh_key_from_disk

	if not server.ipv4_address:
		frappe.throw(f"Server {server.name} has no ipv4_address; cannot SSH")
	path = atlas.get_ssh_private_key_path()
	return Connection(host=server.ipv4_address, ssh_private_key=get_ssh_key_from_disk(path))


def connection_for_guest(virtual_machine) -> Connection:
	"""Build the SSH Connection from a Virtual Machine doc — the second SSH
	target type (the guest, not the host).

	The host path SSHes a Server as root over its public v4 to run staged Tasks.
	This path SSHes a *guest* directly over its public IPv6 `/128`, as root, with
	the SAME Atlas key — the public half is already in the guest's
	`root/.ssh/authorized_keys`, injected by `rootfs.inject_identity()` at
	provision, so no new image plumbing is needed. The control plane (the proxy
	map sync, cert push) uses this to reach a guest's unix-socket admin API over
	SSH. The admin socket's file permissions remain the gate inside the guest;
	SSH-to-the-guest is the only way to reach it.

	A guest is addressed by its public `/128`; sites and the controller are
	generally on different hosts, so there is no host-local shortcut (spec/06:
	no private fabric)."""
	import atlas
	from atlas.atlas.secrets import get_ssh_key_from_disk

	if not virtual_machine.ipv6_address:
		frappe.throw(f"Virtual Machine {virtual_machine.name} has no ipv6_address; cannot SSH to the guest")
	path = atlas.get_ssh_private_key_path()
	return Connection(host=virtual_machine.ipv6_address, ssh_private_key=get_ssh_key_from_disk(path))


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
		stdout, stderr, exit_code = _run_remote_script(connection, script, variables, timeout_seconds)
	except subprocess.TimeoutExpired as timeout:
		_finalize(task, "", f"Timed out after {timeout.timeout}s", None, "Failure", _elapsed_ms(start))
		frappe.throw(f"Task {task.name} timed out after {timeout.timeout}s")
	except Exception as exception:
		_finalize(task, "", str(exception), None, "Failure", _elapsed_ms(start))
		if isinstance(exception, frappe.ValidationError):
			raise
		raise frappe.ValidationError(str(exception)) from exception

	status = "Success" if exit_code == 0 else "Failure"
	_finalize(task, stdout, stderr, exit_code, status, _elapsed_ms(start))
	if status == "Failure":
		# Tail, not head: scripts run under `bash -x`, so the first hundreds of
		# chars are tracing noise and the real error message lives near the end.
		frappe.throw(f"Task {task.name} ({script}) exited {exit_code}: {stderr[-500:]}")


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

	_ensure_known_hosts_directory()

	uploads = files_to_upload(script)
	durable_remote = scripts_catalog.durable_remote_path(script)

	with ssh_key_file(connection.ssh_private_key) as key_path:
		# Fast path: the script is shipped durably at /var/lib/atlas/bin (by
		# bootstrap/sync_scripts, like the atlas package and the systemd hooks) and
		# needs no per-Task sidecar, so invoke it in place — one round trip, no
		# mkdir+scp. This is the bulk of Tasks (every VM lifecycle op); the scp it
		# skips was the dominant latency of an otherwise sub-second start/stop. A
		# host bootstrapped before the durable-scripts cutover lacks the copy and
		# must be re-bootstrapped / sync_scripts'd — the same refresh contract the
		# durable atlas package already follows.
		if durable_remote and not uploads:
			command = _remote_command(script, durable_remote, variables)
			return run_ssh(connection, key_path, command, timeout_seconds=timeout_seconds)

		# Staging path: e2e probe scripts (resolved from the test directory, never
		# shipped durably) and the few scripts with per-Task sidecars (sync-image).
		# Create the staging dir and every remote parent directory the uploads need
		# in one round trip. The purge first: hosts bootstrapped before the
		# durable-package cutover still carry a per-Task staged copy of the lib at
		# <staging>/lib, and the entry points' `sys.path.insert(0, <staging>/lib)`
		# shim puts it AHEAD of PYTHONPATH — so a stale copy there shadows every
		# durable-package update (e.g. an old paths.py without the new attributes).
		# Removing it makes the shim the no-op the durable contract assumes.
		script_path = scripts_catalog.resolve(script)
		remote_dirs = {REMOTE_STAGING_DIRECTORY}
		remote_dirs.update(os.path.dirname(remote) for _, remote in uploads)
		mkdir = (
			f"rm -rf {shlex.quote(STALE_STAGED_PACKAGE_DIRECTORY)} && "
			+ "mkdir -p "
			+ " ".join(shlex.quote(d) for d in sorted(remote_dirs) if d)
		)
		run_ssh(connection, key_path, mkdir, timeout_seconds=60)

		for local, remote in uploads:
			local_path = (scripts_catalog.scripts_directory() / ".." / local).resolve()
			run_scp(connection, key_path, str(local_path), remote, timeout_seconds=300)

		remote_script_path = f"{REMOTE_STAGING_DIRECTORY}/{script}"
		run_scp(connection, key_path, str(script_path), remote_script_path, timeout_seconds=300)

		command = _remote_command(script, remote_script_path, variables)
		return run_ssh(connection, key_path, command, timeout_seconds=timeout_seconds)


def _remote_command(script: str, remote_script_path: str, variables: dict) -> str:
	"""Build the remote invocation for a staged script.

	Python tasks (`.py`) run as `python3 <script> --flag value …`: the variables
	dict maps to CLI flags (UPPER_SNAKE → --kebab-case), the typed-input contract
	the entry points parse with TaskInputs.from_args(). A list value becomes a
	repeated flag (`--cgroup-arg a --cgroup-arg b`). Shell tasks (`.sh`) keep the
	legacy `env VAR=val bash -x <script>` form. Both coexist so the migration can
	proceed script by script.

	The Python form is strictly better for the operator: a Task row now yields a
	runnable, `--help`-able command line, not an `env …` blob.

	Python tasks `import atlas` from the DURABLE package bootstrap placed at
	`/var/lib/atlas/bin/atlas/`, reached via `PYTHONPATH` here — the package is no
	longer re-staged per Task (see script_uploads.py). The entry point's own
	`sys.path.insert(0, <staging>/lib)` shim sits AHEAD of PYTHONPATH, which is
	harmless only because `_run_remote_script` purges <staging>/lib before every
	Task — a legacy host's leftover staged copy would otherwise shadow the
	durable package with stale modules.
	"""
	quoted_path = shlex.quote(remote_script_path)
	if script.endswith(".py"):
		args = _variables_to_flags(variables)
		return f"PYTHONPATH={DURABLE_PACKAGE_DIRECTORY} python3 {quoted_path} {args}".strip()
	env_prefix = " ".join(f"{key}={shlex.quote(str(value))}" for key, value in variables.items())
	return f"env {env_prefix} bash -x {quoted_path}".strip()


def _variables_to_flags(variables: dict) -> str:
	"""Render a variables dict as a CLI argument string: UPPER_SNAKE → --kebab,
	list → repeated flag, everything quoted. Empty/None values are dropped (the
	field's default applies), mirroring the shell's `${VAR:-}` for optionals."""
	parts: list[str] = []
	for key, value in variables.items():
		flag = "--" + key.lower().replace("_", "-")
		if isinstance(value, (list, tuple)):
			for item in value:
				parts += [flag, shlex.quote(str(item))]
		elif value is None or value == "":
			continue
		else:
			parts += [flag, shlex.quote(str(value))]
	return " ".join(parts)
