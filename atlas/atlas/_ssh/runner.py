"""Task lifecycle and remote-script execution on top of `transport.py`."""

import os
import shlex
import subprocess
import time
from typing import TYPE_CHECKING

import frappe
from frappe import _

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

# Where the pre-cutover runner staged the package per Task. The entry points'
# `sys.path.insert(0, <staging>/lib)` shim puts this AHEAD of PYTHONPATH, so a
# leftover copy on a legacy host shadows the durable package with stale modules.
# _run_remote_script purges it before every Task.
STALE_STAGED_PACKAGE_DIRECTORY = f"{REMOTE_STAGING_DIRECTORY}/lib"

# Echoed by the durable-invocation guard when /var/lib/atlas/bin/<script> is
# absent (a host that predates the durable-scripts cutover). The guard gates the
# echo behind `test -f … ||`, so this string appears in a Task's output ONLY when
# the durable copy was missing — never from a script's own run — which lets
# _run_remote_script fall back to staging without misreading a real script's
# output or exit code.
DURABLE_MISSING_MARKER = "__ATLAS_DURABLE_SCRIPT_MISSING__"


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
		frappe.throw(_("run_task: pass exactly one of server= or connection="))

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
	return Connection(host=server.ipv4_address, ssh_private_key=get_ssh_key_from_disk(path), port=22)


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
	# nosemgrep: frappe-manual-commit -- background job: persist the Running state before the long-running SSH operation so a crash mid-run is observable and the Task isn't stuck Queued
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
	# nosemgrep: frappe-manual-commit -- persist the Task outcome before run_task re-raises
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

	# `script` is a VERB (`provision-vm`, `reboot-server`), not a filename — the
	# catalog is the single authority on its nature (python vs shell) and its file.
	uploads = files_to_upload(script)
	verb_kind = scripts_catalog.kind(script)
	durable_remote = scripts_catalog.durable_remote_path(script)

	with ssh_key_file(connection.ssh_private_key) as key_path:
		# Python-verb fast path: run the pip-installed `atlas <verb>` console entry
		# on PATH — no scp, no interpreter path, no PYTHONPATH (install.sh's
		# `uv pip install` put the package and the entry on PATH at bootstrap). This
		# is the bulk of Tasks (every VM lifecycle op), and `bootstrap-server` itself:
		# install.sh creates the venv + console script over SSH BEFORE the bootstrap
		# Task, so by the time it runs `atlas bootstrap-server` dispatches like any
		# other verb — there is no carve-out.
		#
		# A stale/legacy host with no `atlas` on PATH surfaces this as the Task's own
		# `atlas: command not found` — the fail-fast moved from a per-Task `test -e`
		# round trip to once-at-bootstrap (Server.cli_ready, the deep sanity gate).
		# Sidecars (sync-image bakes atlas-network.service) are still staged first.
		if verb_kind == "python":
			if uploads:
				_stage_sidecars(connection, key_path, uploads)
			command = _remote_command(script, None, variables)
			return run_ssh(connection, key_path, command, timeout_seconds=timeout_seconds)

		# Durable-file fast path: the durable shell verbs (reboot-server, `bash -x`
		# by path) are shipped durably at /var/lib/atlas/bin and invoked in place —
		# one round trip, no mkdir+scp. (Python verbs, incl. bootstrap-server, took
		# the `atlas <verb>` fast path above.) A host bootstrapped before the
		# durable-scripts cutover lacks the copy and must be re-bootstrapped /
		# sync_scripts'd — the same refresh contract the durable atlas package follows.
		if durable_remote and not uploads:
			# Guard so a host that predates the durable-scripts cutover (no
			# /var/lib/atlas/bin/<file> yet) degrades to the staging path below for
			# this one Task instead of failing: `test -f` short-circuits to the
			# marker before the file is ever opened, so nothing ran and a staged
			# re-run is safe. The marker can only come from this guard (the echo is
			# gated behind the `||`), so it is an unambiguous fall-back signal. A
			# re-bootstrap / sync_scripts makes the fast path stick.
			inner = _remote_command(script, durable_remote, variables)
			guarded = (
				f"test -f {shlex.quote(durable_remote)} "
				f"|| {{ echo {DURABLE_MISSING_MARKER}; exit 127; }}\n{inner}"
			)
			stdout, stderr, exit_code = run_ssh(
				connection, key_path, guarded, timeout_seconds=timeout_seconds
			)
			if not (exit_code != 0 and DURABLE_MISSING_MARKER in stdout):
				return stdout, stderr, exit_code
			frappe.logger("atlas").warning(
				f"durable script {durable_remote} missing on host; staging this Task — "
				f"re-bootstrap / sync_scripts the server to restore the fast path"
			)
			# fall through to the staging path

		# Staging path: e2e probe scripts (shell verbs resolved from the test
		# directory, never shipped durably) and a durable shell verb whose host copy
		# is missing (the fall-through just above). Create the staging dir and every
		# remote parent directory the uploads need in one round trip. The purge
		# first: hosts bootstrapped before the durable-package cutover still carry a
		# per-Task staged copy of the lib at <staging>/lib, and a stale copy there
		# would shadow every durable-package update; removing it keeps the durable
		# package authoritative.
		script_path = scripts_catalog.resolve(script)
		file_name = scripts_catalog.file_for(script)
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

		remote_script_path = f"{REMOTE_STAGING_DIRECTORY}/{file_name}"
		run_scp(connection, key_path, str(script_path), remote_script_path, timeout_seconds=300)

		command = _remote_command(script, remote_script_path, variables)
		return run_ssh(connection, key_path, command, timeout_seconds=timeout_seconds)


def _stage_sidecars(connection: Connection, key_path: str, uploads: list[tuple[str, str]]) -> None:
	"""Create the staging dir + the sidecars' parent dirs, then scp each sidecar to
	its fixed remote path. Used by the python-verb fast path: the verb itself runs
	as `atlas <verb>`, but a few verbs (sync-image) read an extra file baked at a
	known location. Sidecars are scarce, so this is at most a couple of scps."""
	remote_dirs = {REMOTE_STAGING_DIRECTORY}
	remote_dirs.update(os.path.dirname(remote) for _, remote in uploads)
	mkdir = "mkdir -p " + " ".join(shlex.quote(d) for d in sorted(remote_dirs) if d)
	run_ssh(connection, key_path, mkdir, timeout_seconds=60)
	from atlas.atlas import scripts_catalog

	for local, remote in uploads:
		local_path = (scripts_catalog.scripts_directory() / ".." / local).resolve()
		run_scp(connection, key_path, str(local_path), remote, timeout_seconds=300)


def _remote_command(script: str, remote_script_path: str | None, variables: dict) -> str:
	"""Build the remote invocation for a verb.

	Two shapes, chosen by the catalog's `kind(verb)` (never a suffix-sniff):

	  - Python verb (the bulk, incl. `bootstrap-server`): `atlas <verb> --flag value …`.
	    The pip-installed console script on PATH dispatches to the typed entry point,
	    which parses the flags via TaskInputs.from_args(). `remote_script_path` is
	    None — there is no file path; the install IS the entry. The variables dict
	    maps to CLI flags (UPPER_SNAKE → --kebab-case); a list value becomes a
	    repeated flag. `bootstrap-server` is no longer a carve-out: install.sh creates
	    the venv + console script over SSH before the bootstrap Task.

	  - Shell verb (`reboot-server`, e2e probes): `env VAR=val bash -x <file>`.

	The python form yields a runnable, `--help`-able command line in the Task row,
	not an `env …` blob."""
	from atlas.atlas import scripts_catalog

	if scripts_catalog.kind(script) == "python":
		args = _variables_to_flags(variables)
		return f"atlas {shlex.quote(script)} {args}".strip()
	env_prefix = " ".join(f"{key}={shlex.quote(str(value))}" for key, value in variables.items())
	return f"env {env_prefix} bash -x {shlex.quote(remote_script_path)}".strip()


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
