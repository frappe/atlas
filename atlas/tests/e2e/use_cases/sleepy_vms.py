"""Use case: Sleepy VMs — idle VMs put to sleep, resumed on wake.

Exercises:
1. Auto-provision (insert -> Running)
2. vm.sleep() — memory snapshot + SLEEPING marker on host; status -> Sleeping
3. vm.start() from Sleeping delegates to wake() — status -> Running
4. Repeat stop/sleep to verify idempotency
5. Manual vm.wake() from Sleeping — status -> Running
6. Wake on the first inbound TCP SYN — the whole point of the feature, and the
   only part a tenant ever sees. Covered end to end: parked while asleep, ICMP
   does NOT wake, a TCP connect DOES, unparked afterwards, and the DB catches up.

Host facts checked:
- After sleep: sleeping marker file present, systemd unit inactive, /128 parked
  (named counter + SYN-drop rule + off-link route out atlas-park0)
- After wake: sleeping marker absent, unit active, VM SSH-reachable, unparked
"""

import socket
import subprocess
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
		_check_wake_on_inbound_tcp(server.name)


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


def _tcp_connect(address: str, port: int, timeout: float) -> bool:
	"""One TCP connect attempt to the VM's public /128."""
	try:
		with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
			sock.settimeout(timeout)
			sock.connect((address, port))
		return True
	except OSError:
		return False


def _check_wake_on_inbound_tcp(server_name: str) -> None:
	"""The tenant-visible path: a sleeping VM wakes on the first inbound SYN.

	Runs on a FRESH VM because _check_sleep_wake terminates its own.
	"""
	image_doc = ensure_image_on_server(server_name)
	vm = frappe.get_doc(
		{
			"doctype": "Virtual Machine",
			"title": "sleepy-vm-syn",
			"server": server_name,
			"image": image_doc.name,
			"vcpus": 1,
			"memory_megabytes": 512,
			"disk_gigabytes": 4,
			"ssh_public_key": ephemeral_public_key(),
			"sleep_on_idle": 1,
			"idle_timeout_seconds": 120,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()

	wait_for_vm_running(vm.name, timeout_seconds=180)
	vm.reload()
	address = vm.ipv6_address
	assert address, "VM has no public /128 to dial"

	# The controller must actually be able to reach the guest, or every assertion
	# below would "pass" for the wrong reason.
	assert _tcp_connect(address, 22, timeout=30), (
		f"guest {address}:22 unreachable while Running — "
		"this host/controller pair cannot exercise the SYN trap"
	)

	# --- 1. Sleep, and assert the trap is armed ---
	vm.sleep()
	vm.reload()
	assert vm.status == "Sleeping", vm.status
	assert_probe(server_name, "phase7-is-sleeping", VIRTUAL_MACHINE_NAME=vm.name)
	assert_probe(
		server_name,
		"phase-is-parked",
		timeout_seconds=60,
		VIRTUAL_MACHINE_NAME=vm.name,
		VIRTUAL_MACHINE_IPV6=address,
	)

	# --- 2. ICMP must NOT wake it (the rule matches `tcp flags`, so ping falls
	#        through, is forwarded out the dummy, and is discarded) ---
	subprocess.run(
		["ping6", "-c", "3", "-W", "2", address],
		capture_output=True,
		check=False,
	)
	time.sleep(5)
	assert_probe(server_name, "phase7-is-sleeping", VIRTUAL_MACHINE_NAME=vm.name)
	vm.reload()
	assert vm.status == "Sleeping", f"ICMP woke the VM; the trap is not TCP-only ({vm.status})"

	# --- 3. A TCP SYN DOES wake it. The first SYN is dropped by design, so the
	#        connect fails and the kernel retransmits (~1s) into the resumed
	#        guest; retry until the guest answers. ---
	deadline = time.monotonic() + 120
	connected = False
	while time.monotonic() < deadline:
		if _tcp_connect(address, 22, timeout=5):
			connected = True
			break
		time.sleep(2)
	assert connected, f"{address}:22 never answered after the SYN trap should have woken it"

	assert_probe(server_name, "phase5-is-active", VIRTUAL_MACHINE_NAME=vm.name)
	assert_probe(
		server_name,
		"phase-is-unparked",
		timeout_seconds=60,
		VIRTUAL_MACHINE_NAME=vm.name,
		VIRTUAL_MACHINE_IPV6=address,
	)

	# --- 4. The DB catches up. The host woke the VM on its own; Atlas learns
	#        about it from the per-minute reconcile, not from a Task. ---
	from atlas.atlas.doctype.virtual_machine.virtual_machine import reconcile_sleeping_vms

	reconcile_sleeping_vms()
	vm.reload()
	assert vm.status == "Running", f"reconcile did not adopt the host wake ({vm.status})"
	assert not vm.has_memory_snapshot, "the wake consumed the memory snapshot"
	assert vm.last_traffic_at, "adopting the wake must reset the idle clock"

	vm.terminate()
