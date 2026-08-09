"""Use case: operate a running Firecracker VM.

Operator clicks Start / Stop / Restart / Terminate. Each is one Task
(`start-vm.py`, `snapshot-stop-vm.py` — the default memory-capturing stop —
`stop-vm.py`, `terminate-vm.py`); Restart is `stop` + `start` orchestrated in
Python, not a separate script.

This module exercises:

- Auto-provision (insert -> Running) -> Stop -> Start -> Restart ->
  Terminate, with a probe on every state.
- The memory-snapshot fast path on the way: the VM is OPTED IN
  (memory_snapshot_on_stop — the plain stop stays the default), so the stop
  captures the guest's memory (`has_memory_snapshot`) and the next start
  RESUMES it instead of cold-booting (the start Task reports "restored from
  memory snapshot") — the host fact only a real Firecracker round trip can
  prove.
- Terminate again from Terminated throws.

(Phase 4 dropped the Pending state guards from this use case — auto-provision
fires from `after_insert`, so a freshly-inserted VM races to Running and
won't dwell in Pending long enough for an operator to mis-click anyway. The
controller still enforces the guards in `start()` / `stop()` / `restart()`;
those branches are exercised by the unit-test suite.)
"""

import time

import frappe

from atlas.tests.e2e._shared import (
	assert_probe,
	ensure_image_on_server,
	ephemeral_public_key,
	expect_validation_error,
	phase,
	wait_for_vm_running,
)


def run(reuse: bool = True, keep: bool = True) -> None:
	with phase("vm-lifecycle", reuse=reuse, keep=keep) as server:
		image_doc = ensure_image_on_server(server.name)
		public_key = ephemeral_public_key()

		vm = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"title": "vm-lifecycle",
				"server": server.name,
				"image": image_doc.name,
				"vcpus": 1,
				"memory_megabytes": 512,
				"disk_gigabytes": 4,
				"ssh_public_key": public_key,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		_check_full_lifecycle(server.name, vm)


# The lifecycle IS the host fact — every state transition is probed on-host and
# only a real VM can prove them. The lone unit-dup (terminate-already-terminated
# throw, covered by `virtual_machine/test_virtual_machine_lifecycle.py`) is an
# in-memory check at the tail with no host cost, so the smoke path is the full
# run rather than a trimmed one.
run_smoke = run


def _check_full_lifecycle(server_name: str, vm) -> None:
	"""Auto-provision (insert -> Running) -> Stop -> Start -> Restart ->
	Terminate, with probes."""
	# Phase 4 auto-provision contract: after_insert enqueued provision();
	# wait for the worker rather than calling vm.provision() directly.
	wait_for_vm_running(vm.name, timeout_seconds=120)
	vm.reload()
	assert vm.status == "Running", vm.status
	first_started = vm.last_started
	assert_probe(server_name, "phase5-is-active", VIRTUAL_MACHINE_NAME=vm.name)

	# Stop, opted into the memory-snapshot fast path (the plain stop is the
	# default; this VM opts in to prove the capture/restore round trip). A
	# freshly-provisioned VM's launcher supports the restore, so the fast path
	# must actually engage — a silent fallback to the plain stop would pass
	# every other assert and hide a broken snapshot path.
	vm.db_set("memory_snapshot_on_stop", 1)
	vm.reload()
	vm.stop()
	vm.reload()
	assert vm.status == "Stopped", vm.status
	assert vm.last_stopped, "last_stopped should be set"
	assert vm.has_memory_snapshot, "default stop should have captured a memory snapshot"
	assert_probe(server_name, "phase6-is-inactive", VIRTUAL_MACHINE_NAME=vm.name)

	# Start — must RESUME from the memory snapshot, not cold-boot. The start
	# Task says which way it went; the flag is consumed either way.
	time.sleep(1)  # advance clock for last_started comparison
	start_task = frappe.get_doc("Task", vm.start())
	vm.reload()
	assert vm.status == "Running", vm.status
	assert vm.last_started > first_started, (
		f"last_started did not advance: {first_started} -> {vm.last_started}"
	)
	assert "restored from memory snapshot" in (start_task.stdout or ""), (
		f"start did not restore the memory snapshot:\n{start_task.stdout}"
	)
	assert not vm.has_memory_snapshot, "the start should have consumed the snapshot"
	assert_probe(server_name, "phase5-is-active", VIRTUAL_MACHINE_NAME=vm.name)

	# Restart (Running -> Running, two tasks).
	before_stop = vm.last_stopped
	before_start = vm.last_started
	time.sleep(1)
	result = vm.restart()
	assert result["stop_task"] and result["start_task"], result
	vm.reload()
	assert vm.status == "Running", vm.status
	assert vm.last_stopped > before_stop, "last_stopped did not advance on restart"
	assert vm.last_started > before_start, "last_started did not advance on restart"
	assert_probe(server_name, "phase5-is-active", VIRTUAL_MACHINE_NAME=vm.name)

	# Terminate.
	tap_device = vm.tap_device
	vm.terminate()
	vm.reload()
	assert vm.status == "Terminated", vm.status
	assert_probe(
		server_name,
		"phase6-assert-gone",
		VIRTUAL_MACHINE_NAME=vm.name,
		TAP_DEVICE=tap_device,
	)

	# Terminate from Terminated -> throw.
	with expect_validation_error("already terminated"):
		vm.terminate()
