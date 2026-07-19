"""Controller-local Task runner — the non-SSH sibling of `_ssh/runner.run_task`.

A handful of operations belong to the *controller*, not a host: certificate
issuance is the first (the ACME client runs where the PEMs land, and there is no
remote host to stage a script onto). This runner executes one of the repo's
`scripts/*.py` Tasks **as a local subprocess**, still records a `Task` row (the
operator sees it in the same audit list as every host/guest op), and returns the
typed `ATLAS_RESULT=` payload via `atlas.atlas.task_results.parse_result`.

It deliberately reuses the same building blocks as the SSH path:
  - `scripts_catalog.resolve()` to locate the script,
  - the `--kebab-case` flag contract (`_variables_to_flags` from the runner),
  - `parse_result()` for the typed result line,
so a controller Task is byte-for-byte the same script the host path would run —
only the transport (subprocess vs ssh) differs. Secrets go through `env`, never
argv, so they never appear in `ps`.
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import TYPE_CHECKING

import frappe

from atlas.atlas import scripts_catalog

if TYPE_CHECKING:
	from atlas.atlas.doctype.task.task import Task


def run_local_task(
	*,
	script: str,
	variables: dict,
	env: dict[str, str] | None = None,
	virtual_machine: str | None = None,
	timeout_seconds: int = 1800,
) -> "Task":
	"""Run `script` locally as `python3 <script> --flags …`, recording a Task row.

	`variables` maps UPPER_SNAKE → value, rendered to `--kebab-case` flags exactly
	as the SSH runner does. `env` carries vendor secrets (e.g. AWS creds) into the
	subprocess environment — kept out of argv on purpose. Raises
	frappe.ValidationError on any failure; the Task row is always saved first.
	"""
	task = frappe.get_doc(
		{
			"doctype": "Task",
			"virtual_machine": virtual_machine,
			"script": script,
			"status": "Pending",
			"triggered_by": frappe.session.user if frappe.session else "Administrator",
		}
	)
	task.variables_dict = variables
	task.insert(ignore_permissions=True)

	_execute_local(task, script, variables, env or {}, timeout_seconds)
	return task


def _execute_local(
	task: "Task",
	script: str,
	variables: dict,
	env: dict[str, str],
	timeout_seconds: int,
) -> None:
	task.status = "Running"
	task.started = frappe.utils.now_datetime()
	task.save(ignore_permissions=True)
	# nosemgrep: frappe-manual-commit -- background job: persist the Running state before the long-running local subprocess so a crash mid-run is observable and the Task isn't stuck Queued
	frappe.db.commit()

	start = time.monotonic()
	try:
		stdout, stderr, exit_code = _run_local_script(script, variables, env, timeout_seconds)
	except subprocess.TimeoutExpired as timeout:
		_finalize(task, "", f"Timed out after {timeout.timeout}s", None, "Failure", _elapsed_ms(start))
		frappe.throw(f"Task {task.name} timed out after {timeout.timeout}s")
	except Exception as exception:
		_finalize(task, "", str(exception), None, "Failure", _elapsed_ms(start))
		raise frappe.ValidationError(str(exception)) from exception

	status = "Success" if exit_code == 0 else "Failure"
	_finalize(task, stdout, stderr, exit_code, status, _elapsed_ms(start))
	if status == "Failure":
		frappe.throw(f"Task {task.name} ({script}) exited {exit_code}: {stderr[-500:]}")


def _run_local_script(
	script: str,
	variables: dict,
	env: dict[str, str],
	timeout_seconds: int,
) -> tuple[str, str, int]:
	import os

	script_path = scripts_catalog.resolve(script)
	# `script` is a VERB (`issue-cert`); resolve() maps it to the file. Controller
	# Tasks invoke `[sys.executable, <file>, …]` by path, not the host's `atlas`
	# console script — the controller runs from the repo tree and these
	# controller-only verbs never had a console entry. The script and its
	# `scripts/lib/atlas` package are already on the controller's disk, so we invoke
	# them in place.
	argv = [sys.executable, str(script_path), *_variables_to_argv(variables)]
	subprocess_env = {**os.environ, **env}
	result = subprocess.run(
		argv,
		capture_output=True,
		text=True,
		timeout=timeout_seconds,
		check=False,
		env=subprocess_env,
	)
	return result.stdout, result.stderr, result.returncode


def _variables_to_argv(variables: dict) -> list[str]:
	"""Render Task variables as a real argv vector.

	Unlike the SSH path, local tasks call subprocess directly, so list values must
	not go through a shell string and shlex.split(). Repeated flags whose values
	look like options (for example --certbot-arg=--authenticator) need the
	--flag=value form or argparse treats the value as a new option.
	"""
	argv: list[str] = []
	for key, value in variables.items():
		flag = "--" + key.lower().replace("_", "-")
		if isinstance(value, (list, tuple)):
			for item in value:
				argv.append(f"{flag}={item}")
		elif value is None or value == "":
			continue
		else:
			argv.extend([flag, str(value)])
	return argv


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
	# nosemgrep: frappe-manual-commit -- persist the Task outcome before run_local_task re-raises so the final status survives the raise
	frappe.db.commit()


def _elapsed_ms(start: float) -> int:
	return int((time.monotonic() - start) * 1000)
