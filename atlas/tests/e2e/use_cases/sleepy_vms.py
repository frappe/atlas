"""Use case: Sleepy VMs — idle VMs put to sleep, resumed on wake.

Exercises:
1. Auto-provision (insert -> Running)
2. vm.sleep() — memory snapshot + SLEEPING marker on host; status -> Sleeping
3. vm.start() from Sleeping delegates to wake() — status -> Running
4. Repeat stop/sleep to verify idempotency
5. Manual vm.wake() from Sleeping — status -> Running

Host facts checked:
- After sleep: sleeping marker file present, systemd unit inactive
- After wake: sleeping marker file absent, systemd unit active, VM SSH-reachable
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
	with phase("sleepy-vms", reuse=reuse, keep=keep) as server:
		image_doc = ensure_image_on_server(server.name)
		public_key = ephemeral_public_key()

		vm = frappe.get_doc(
			{
				"doctype": "Virtual Machine",
				"title": "sleepy-vm",
				"server": server.name,
				"image": image_doc.name,
				"vcpus": 1,
				"memory_megabytes": 512,
				"disk_gigabytes": 4,
				"ssh_public_key": public_key,
				"sleep_on_idle": 1,
				"idle_timeout_seconds": 120,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		_check_sleep_wake(server.name, vm)


run_smoke = run


def _check_sleep_wake(server_name: str, vm) -> None:
	# --- 1. Auto-provision -> Running ---
	wait_for_vm_running(vm.name, timeout_seconds=120)
	vm.reload()
	assert vm.status == "Running", vm.status
	assert_probe(server_name, "phase5-is-active", VIRTUAL_MACHINE_NAME=vm.name)

	# --- 2. Sleep ---
	vm.sleep()
	vm.reload()
	assert vm.status == "Sleeping", f"expected Sleeping, got {vm.status}"
	assert vm.has_memory_snapshot, "sleep() should have captured a memory snapshot"
	assert vm.last_stopped, "last_stopped should be set after sleep()"

	# Host facts: sleeping marker present, unit inactive
	assert_probe(server_name, "phase7-is-sleeping", VIRTUAL_MACHINE_NAME=vm.name)

	# --- 3. Guards while sleeping ---
	with expect_validation_error("sleeping"):
		vm.stop()
	with expect_validation_error("sleeping"):
		vm.snapshot()

	# --- 4. start() from Sleeping delegates to wake() ---
	time.sleep(1)
	vm.start()  # must call wake() internally
	vm.reload()
	assert vm.status == "Running", f"expected Running after start-from-Sleeping, got {vm.status}"
	assert not vm.has_memory_snapshot, "wake should clear has_memory_snapshot"
	assert vm.last_started, "last_started should be set"

	# Host facts: sleeping marker gone, unit active
	assert_probe(server_name, "phase5-is-active", VIRTUAL_MACHINE_NAME=vm.name)

	# --- 5. Sleep again, then explicit wake() ---
	vm.sleep()
	vm.reload()
	assert vm.status == "Sleeping", vm.status

	assert_probe(server_name, "phase7-is-sleeping", VIRTUAL_MACHINE_NAME=vm.name)

	time.sleep(1)
	vm.wake()
	vm.reload()
	assert vm.status == "Running", f"expected Running after wake(), got {vm.status}"
	assert_probe(server_name, "phase5-is-active", VIRTUAL_MACHINE_NAME=vm.name)

	# --- 6. wake() from non-Sleeping throws ---
	with expect_validation_error("Cannot wake"):
		vm.wake()

	# --- 7. Terminate ---
	vm.terminate()
	vm.reload()
	assert vm.status == "Terminated", vm.status
