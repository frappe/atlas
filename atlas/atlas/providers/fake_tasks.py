"""The Fake provider's task seam — succeed (or fail) a Task without SSH.

The `Provider` ABC only covers server creation; every *Virtual Machine* action,
image sync, and `Server.bootstrap()` runs as a Task over SSH through
`run_task()`. So `run_task`/`execute_task` consult `is_fake_server()` and, for a
Fake-backed Server, hand off here instead of opening a real `Connection`.

`run_fake_task()` builds a Task row that is byte-for-byte what a successful real
run produces — `Pending → Running → Success`, with a synthetic
`ATLAS_RESULT=<json>` line for the four scripts whose controllers parse one
(`task_results.parse_result` raises if it is missing). Fault injection turns any
script into a `Failure` exactly the way a real non-zero exit would, so the
controller's error path (VM → `Failed`, Server → `Broken`, retry button) runs
unchanged.

Routing is per-Server: `is_fake_server` keys off the Server row's own provider,
not the globally-active one, so a Fake provider and a historical real Server can
coexist and each Task goes the right way.
"""

from __future__ import annotations

import dataclasses
import json
import time
from typing import TYPE_CHECKING

import frappe

from atlas.atlas.providers.fake import FAKE_PROVIDER_TYPE

if TYPE_CHECKING:
	from atlas.atlas.doctype.task.task import Task

RESULT_MARKER = "ATLAS_RESULT="


def is_fake_server(server_name: str | None) -> bool:
	"""True iff `server_name`'s provider_type is Fake. One cheap read."""
	if not server_name:
		return False
	return frappe.db.get_value("Server", server_name, "provider_type") == FAKE_PROVIDER_TYPE


@dataclasses.dataclass(frozen=True)
class _Failure:
	reason: str


def run_fake_task(
	server: str,
	script: str,
	variables: dict,
	virtual_machine: str | None,
) -> "Task":
	"""Create and finalize a Task for a Fake server, with no SSH (the
	synchronous `run_task` path).

	Mirrors the real `run_task` outcome contract: the Task row is always saved
	with its outcome, and a failure raises `frappe.ValidationError` after the row
	is marked `Failure` — identical to a real non-zero exit."""
	task = _insert_fake_task(server, script, variables, virtual_machine)
	finalize_fake_task(task)
	return task


def finalize_fake_task(task: "Task") -> None:
	"""Drive an already-inserted Pending Task to its fake outcome (the
	`execute_task` background-job path). Shares the synthesis with
	`run_fake_task`, so both paths land identical rows."""
	from atlas.atlas._ssh.runner import _finalize, _mark_running

	_mark_running(task)
	start = time.monotonic()
	failure = _should_fail(task.server, task.script)
	if failure is not None:
		_finalize(task, "", failure.reason, 1, "Failure", _elapsed_ms(start))
		frappe.throw(f"Task {task.name} ({task.script}) failed (fake): {failure.reason}")
	stdout = _fake_stdout(task.script, task.variables_dict)
	_finalize(task, stdout, "", 0, "Success", _elapsed_ms(start))


def _insert_fake_task(server: str, script: str, variables: dict, virtual_machine: str | None) -> "Task":
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
	return task


def _elapsed_ms(start: float) -> int:
	# Real fake tasks are sub-millisecond; report a small non-zero duration so a
	# seeded Task list doesn't show a column of zeros.
	return max(1, int((time.monotonic() - start) * 1000))


# --- Fault injection -----------------------------------------------------
# Two converging sources, simplest first:
#   1. `frappe.flags.fake_fail` — a per-call override for tests. Either a script
#      name (str), an iterable of names, or a dict {"script": ..., "reason": ...}.
#   2. `Atlas Settings.fail_scripts` — a persistent, developer-facing list on the
#      Single (comma/newline separated, or "*" for every script). Only meaningful
#      when provider_type is Fake.


def _should_fail(server: str, script: str) -> _Failure | None:
	flagged = _flag_failure(script)
	if flagged is not None:
		return flagged
	return _configured_failure(script)


def _flag_failure(script: str) -> _Failure | None:
	spec = frappe.flags.get("fake_fail") if frappe.flags else None
	if not spec:
		return None
	if isinstance(spec, dict):
		target = spec.get("script")
		if target in ("*", script):
			return _Failure(spec.get("reason") or f"forced failure for {script}")
		return None
	if isinstance(spec, str):
		names = {spec}
	else:
		names = set(spec)
	if "*" in names or script in names:
		return _Failure(f"forced failure for {script}")
	return None


def _configured_failure(script: str) -> _Failure | None:
	raw = frappe.db.get_single_value("Atlas Settings", "fail_scripts")
	names = _parse_script_list(raw)
	if "*" in names or script in names:
		return _Failure(f"Fake provider configured to fail {script}")
	return None


def _parse_script_list(raw: str | None) -> set[str]:
	if not raw:
		return set()
	return {token.strip() for token in raw.replace(",", "\n").splitlines() if token.strip()}


# --- Synthetic stdout ----------------------------------------------------
# Only four scripts have a result the controller reads back (see
# task_results.parse_result); the rest may emit a plain line. The fake produces
# a valid ATLAS_RESULT for those four so the controller never throws.


def _fake_stdout(script: str, variables: dict) -> str:
	builder = _RESULT_BUILDERS.get(script)
	if builder is None:
		return "ok\n"
	return RESULT_MARKER + json.dumps(builder(variables)) + "\n"


def _bootstrap_result(_variables: dict) -> dict:
	return {
		"firecracker_version": "v1.16.0",
		"jailer_version": "v1.16.0",
		"kernel_version": "6.1.0-fake",
		"architecture": "x86_64",
		# The Atlas venv python the real bootstrap resolves (display-only — no
		# Server field backs it). Mirror the real BootstrapResult shape so a fake
		# host's result line is byte-shaped like a real one.
		"python_version": "Python 3.14.3",
	}


def _snapshot_result(variables: dict) -> dict:
	# size_bytes is read; data_size_bytes only when the VM had a data disk.
	has_data = bool(variables.get("DATA_SNAPSHOT_ROOTFS_PATH"))
	return {"size_bytes": _fake_disk_bytes(variables), "data_size_bytes": 2_000_000_000 if has_data else 0}


def _snapshot_stop_result(_variables: dict) -> dict:
	return {"memory_snapshot": True, "reason": "", "memory_snapshot_bytes": 536_870_912}


def _warm_snapshot_result(variables: dict) -> dict:
	return {
		"size_bytes": _fake_disk_bytes(variables),
		"memory_bytes": 536_870_912,
		"host_signature": json.dumps(
			{
				"cpu": "fake",
				"flags": "fake",
				"microcode": "0x0",
				"kernel": "6.1.0-fake",
				"firecracker": "v1.16.0",
			}
		),
	}


def _server_facts_result(_variables: dict) -> dict:
	# A Fake host measures nothing, so report the DEFAULT fake size's totals — enough
	# that the Refresh Capacity button doesn't crash `parse_result` in dev. Cosmetic:
	# a Fake host's real capacity comes from `fake_host_totals` in
	# `capacity_for_server`, which re-synthesizes and ignores the stamped row.
	from atlas.atlas.providers.fake import DEFAULT_FAKE_SIZE, fake_host_totals

	return {**fake_host_totals(DEFAULT_FAKE_SIZE), "pool_data_percent": 0.0}


def _fake_disk_bytes(variables: dict) -> int:
	"""A plausible rootfs size: the VM's disk in bytes, defaulting to ~4 GB."""
	try:
		gigabytes = int(variables.get("DISK_GB") or 4)
	except (TypeError, ValueError):
		gigabytes = 4
	return gigabytes * 1024 * 1024 * 1024


_RESULT_BUILDERS = {
	"bootstrap-server": _bootstrap_result,
	"server-facts": _server_facts_result,
	"snapshot-vm": _snapshot_result,
	"snapshot-stop-vm": _snapshot_stop_result,
	"warm-snapshot-vm": _warm_snapshot_result,
}
