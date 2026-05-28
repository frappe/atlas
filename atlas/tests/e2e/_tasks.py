"""Task-row helpers for the e2e harness."""

import time
from contextlib import contextmanager

import frappe


@contextmanager
def expect_validation_error(*needles: str):
	"""Assert the wrapped block raises a frappe.ValidationError whose lowercased
	message contains at least one of `needles` (also lowercased)."""
	try:
		yield
	except frappe.ValidationError as exception:
		message = str(exception).lower()
		if not any(needle.lower() in message for needle in needles):
			raise AssertionError(
				f"ValidationError did not contain any of {needles}: {message}"
			) from exception
		return
	raise AssertionError(
		f"expected frappe.ValidationError containing {needles}, no exception raised"
	)


def wait_for_task(
	task_name: str,
	timeout_seconds: int,
	poll_seconds: float = 1.0,
) -> "frappe.model.document.Document":
	"""Poll a Task row to Success or Failure, or AssertionError on timeout.

	Also raises if the row sits in Running well past its own declared timeout,
	which means the worker died between "set Running" and the final update.
	"""
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		task = frappe.get_doc("Task", task_name)
		if task.status in ("Success", "Failure"):
			return task
		if task.status == "Running" and task.started:
			age = (frappe.utils.now_datetime() - task.started).total_seconds()
			if age > 2 * timeout_seconds:
				raise AssertionError(
					f"task {task_name} is orphaned (Running for {age:.0f}s, "
					f"declared timeout {timeout_seconds}s)"
				)
		time.sleep(poll_seconds)
	raise AssertionError(f"task {task_name} did not finish within {timeout_seconds}s")


def wait_for_vm_running(
	virtual_machine_name: str,
	timeout_seconds: int = 60,
	poll_seconds: float = 1.0,
) -> "frappe.model.document.Document":
	"""Wait for `after_insert` auto-provision to flip a VM to Running.

	Phase 4's auto-provision contract: inserting a Virtual Machine row
	enqueues `provision()` from `after_insert`. Callers no longer click
	the Provision button; instead they wait for the background worker to
	drive the state transition. Raises if the VM is still Pending past
	the deadline (worker didn't pick up the job) or lands in Failed.
	"""
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline:
		frappe.db.rollback()
		vm = frappe.get_doc("Virtual Machine", virtual_machine_name)
		if vm.status == "Running":
			return vm
		if vm.status == "Failed":
			raise AssertionError(
				f"VM {virtual_machine_name} reached Failed during auto-provision"
			)
		time.sleep(poll_seconds)
	raise AssertionError(
		f"VM {virtual_machine_name} did not reach Running within {timeout_seconds}s "
		f"(auto-provision worker likely didn't run)"
	)


def mark_orphan_tasks_failure(older_than_minutes: int = 10) -> int:
	"""Mark Running Tasks older than N minutes as Failure. Safety net for
	workers that died mid-job. Returns count marked.
	"""
	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-older_than_minutes)
	stuck = frappe.get_all(
		"Task",
		filters={"status": "Running", "started": ["<", cutoff]},
		pluck="name",
	)
	for name in stuck:
		doc = frappe.get_doc("Task", name)
		doc.status = "Failure"
		doc.stderr = (doc.stderr or "") + (
			f"\n[atlas e2e] marked Failure: Running for >{older_than_minutes} min "
			"(worker presumed dead)\n"
		)
		doc.ended = frappe.utils.now_datetime()
		doc.save(ignore_permissions=True)
	frappe.db.commit()
	if stuck:
		print(f"[e2e] marked {len(stuck)} orphan Task(s) as Failure")
	return len(stuck)


def assert_probe(
	server_name: str,
	script: str,
	timeout_seconds: int = 15,
	**variables: str,
) -> None:
	"""Run `script` on `server_name` and assert it exits Success.

	The probe's success is the assertion — its script is expected to `exit 0`
	when the condition holds and non-zero otherwise. Probes that need to wait
	on something (e.g. a guest VM booting) bump `timeout_seconds`.
	"""
	from atlas.atlas.ssh import run_task

	task = run_task(
		server=server_name,
		script=script,
		variables=variables,
		timeout_seconds=timeout_seconds,
	)
	assert task.status == "Success", task.stderr
