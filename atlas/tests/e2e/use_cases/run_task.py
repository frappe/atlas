"""Use case: run an ad-hoc Task or reboot a server.

`Server.run_task_dialog(script, variables)` is the escape hatch on the
Server form. It runs any script in the catalogue with operator-supplied
variables and returns the new Task name. `Server.reboot()` is the same
shape with `reboot-server.sh` hard-coded; its Task may end Success or
Failure depending on whether `systemctl reboot` exits before SSH drops.

This module exercises:

- run_task_dialog with a real script (idempotent re-bootstrap).
- run_task_dialog argument shapes: unknown script throws; `variables=None`
  defaults to `{}`; `variables` as a JSON string round-trips; `variables`
  as a non-object throws.
- Server.reboot returns a Task and SSH does come back.
- The Task DocType's own validation: empty / malformed / non-object
  `variables`; setter rejects non-dict; immutability after insert.
- Direct `run_task` failure modes: unknown script, exit non-zero, timeout.
  These ensure the Task row is finalized as Failure on every error path —
  no half-written rows.
"""

import time

import frappe

from atlas.atlas._ssh.runner import run_task
from atlas.tests.e2e._shared import (
	expect_validation_error,
	phase,
	server_is_reachable,
)


def run(reuse: bool = True, keep: bool = True) -> None:
	"""Full path: HOST run/failure/timeout/reboot + the dialog and Task-DocType
	validation the unit suite also covers. See [run_smoke](#run_smoke)."""
	with phase("run-task", reuse=reuse, keep=keep) as server:
		_check_run_task_dialog_happy(server)
		_check_run_task_dialog_argument_shapes(server)
		_check_task_doctype_validation(server)
		_check_run_task_unknown_script(server)
		_check_run_task_remote_failure(server)
		_check_run_task_timeout(server)
		_check_reboot(server)


def run_smoke(reuse: bool = True, keep: bool = True, reboot: bool = False) -> None:
	"""Host-only path for development. Runs a real script, drives the
	remote-failure and remote-timeout branches (exit code + Failure row), which
	need a live host to prove.

	`reboot=False` by default: `_check_reboot` is a flat 30s drop + up to 300s
	reconnect poll — the most expensive non-provision wait in the suite. Pass
	`reboot=True` when you touched `reboot-server.sh` or the reconnect path.

	Skips dialog argument shapes and Task DocType validation (pure logic,
	covered by `test_ssh_runner.py`, `task/test_task.py`,
	`server/test_server_runtask.py`)."""
	with phase("run-task (smoke)", reuse=reuse, keep=keep) as server:
		_check_run_task_dialog_happy(server)
		_check_run_task_remote_failure(server)
		_check_run_task_timeout(server)
		if reboot:
			_check_reboot(server)


def _check_run_task_dialog_happy(server) -> None:
	"""Re-bootstrap via run_task_dialog — same code path as bootstrap()."""
	task_name = server.run_task_dialog(
		script="bootstrap-server.sh",
		variables={
			"FIRECRACKER_VERSION": "v1.15.1",
			"ARCHITECTURE": "x86_64",
		},
	)
	task = frappe.get_doc("Task", task_name)
	assert task.status == "Success", task.stderr
	assert task.script == "bootstrap-server.sh"
	assert task.server == server.name


def _check_run_task_dialog_argument_shapes(server) -> None:
	"""variables= accepts dict, None, or JSON string; non-object throws.

	The happy path is covered above; here we drive the pre-flight
	branches by pairing each variables= shape with an unknown script, so
	the throw happens *after* the parse branch.
	"""
	with expect_validation_error("unknown script"):
		server.run_task_dialog(script="not-a-real-script.sh", variables={})
	with expect_validation_error("unknown script"):
		server.run_task_dialog(script="not-a-real-script.sh", variables=None)
	with expect_validation_error("unknown script"):
		server.run_task_dialog(script="not-a-real-script.sh", variables='{"X": "1"}')
	with expect_validation_error("variables must"):
		server.run_task_dialog(script="not-a-real-script.sh", variables="[1, 2]")


def _check_task_doctype_validation(server) -> None:
	"""Task is read-only after insert; variables must be a JSON object."""
	# variables empty -> throw.
	doc = frappe.get_doc({
		"doctype": "Task",
		"server": server.name,
		"script": "noop.sh",
		"status": "Pending",
		"triggered_by": "Administrator",
	})
	with expect_validation_error("variables is required"):
		doc.insert(ignore_permissions=True)

	# variables not JSON -> throw.
	doc = frappe.get_doc({
		"doctype": "Task",
		"server": server.name,
		"script": "noop.sh",
		"status": "Pending",
		"triggered_by": "Administrator",
		"variables": "not json",
	})
	with expect_validation_error("must be valid json"):
		doc.insert(ignore_permissions=True)

	# variables not an object -> throw.
	doc = frappe.get_doc({
		"doctype": "Task",
		"server": server.name,
		"script": "noop.sh",
		"status": "Pending",
		"triggered_by": "Administrator",
		"variables": "[1, 2]",
	})
	with expect_validation_error("json object"):
		doc.insert(ignore_permissions=True)

	# variables_dict setter rejects non-dict.
	doc = frappe.get_doc({
		"doctype": "Task",
		"server": server.name,
		"script": "noop.sh",
		"status": "Pending",
		"triggered_by": "Administrator",
	})
	with expect_validation_error("must be a dict"):
		doc.variables_dict = [1, 2]

	# Immutability: mutate `script` after insert.
	doc = frappe.get_doc({
		"doctype": "Task",
		"server": server.name,
		"script": "phase1-probe.sh",
		"status": "Pending",
		"triggered_by": "Administrator",
	})
	doc.variables_dict = {"NAME": "x"}
	doc.insert(ignore_permissions=True)
	doc.reload()
	doc.script = "phase1-fail.sh"
	with expect_validation_error("read-only after insert"):
		doc.save(ignore_permissions=True)

	# Reader returns the parsed dict.
	doc2 = frappe.get_doc("Task", doc.name)
	assert doc2.variables_dict == {"NAME": "x"}, doc2.variables_dict

	# _validate_immutability early-returns when _doc_before_save is None.
	fresh = frappe.get_doc("Task", doc.name)
	fresh._validate_immutability()


def _check_run_task_unknown_script(server) -> None:
	"""run_task with a missing script raises and finalizes the row Failure."""
	with expect_validation_error("not found"):
		run_task(
			server=server.name,
			script="usecase-unknown-script.sh",
			variables={"X": "1"},
			timeout_seconds=10,
		)
	task = frappe.get_last_doc("Task", filters={"script": "usecase-unknown-script.sh"})
	assert task.status == "Failure", task.status


def _check_run_task_remote_failure(server) -> None:
	"""Remote script exits non-zero -> ValidationError; Task row is Failure."""
	with expect_validation_error("exited"):
		run_task(
			server=server.name,
			script="phase1-fail.sh",
			variables={},
			timeout_seconds=10,
		)
	task = frappe.get_last_doc("Task", filters={"script": "phase1-fail.sh"})
	assert task.status == "Failure", task.status
	assert task.exit_code == 7, task.exit_code


def _check_run_task_timeout(server) -> None:
	"""Remote script outruns the timeout -> ValidationError; row is Failure."""
	with expect_validation_error("timed out"):
		run_task(
			server=server.name,
			script="phase8-sleep.sh",
			variables={},
			timeout_seconds=2,
		)
	task = frappe.get_last_doc("Task", filters={"script": "phase8-sleep.sh"})
	assert task.status == "Failure", task.status
	assert "timed out" in (task.stderr or "").lower(), task.stderr


def _check_reboot(server) -> None:
	"""server.reboot() returns a Task; SSH drops then comes back."""
	reboot_task_name = server.reboot()
	reboot_task = frappe.get_doc("Task", reboot_task_name)
	assert reboot_task.script == "reboot-server.sh"
	# Either Failure (SSH drops mid-task) or Success (systemctl exits before
	# the connection is torn down). Both are normal; we care that SSH comes
	# back.
	assert reboot_task.status in ("Failure", "Success"), reboot_task.status

	print("[run-task] waiting 30s for server to begin reboot...")
	time.sleep(30)

	print("[run-task] probing server until SSH responds (up to 5 min)...")
	deadline = time.monotonic() + 300
	while time.monotonic() < deadline:
		if server_is_reachable(server.name, timeout_seconds=10):
			break
		time.sleep(5)
	else:
		raise AssertionError("server did not come back within 5 minutes")
