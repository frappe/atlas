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

import time

import frappe

from atlas.atlas.doctype.virtual_machine.virtual_machine import (
	reconcile_sleeping_vms,
)
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

		# The SYN trap needs an off-host vantage (see _check_wake_on_inbound_tcp).
		# Without a second reachable host the scenario cannot be exercised
		# honestly, so say so loudly and skip it rather than pass vacuously.
		vantage = _find_vantage(server.name)
		if vantage:
			_check_wake_on_inbound_tcp(server.name, vantage)
		else:
			print(
				"sleepy-vms: SKIPPED wake-on-inbound-TCP — no second Active, "
				"SSH-reachable host to dial from. Run the two-host e2e, or use "
				"run_with_vantage(<server-name>)."
			)


run_smoke = run


def run_with_vantage(vantage_name: str, reuse: bool = True, keep: bool = True) -> None:
	"""Run only the wake-on-inbound-TCP scenario, dialing from `vantage_name`."""
	with phase("sleepy-vms-syn", reuse=reuse, keep=keep) as server:
		assert vantage_name != server.name, "the vantage must not be the VM's own host"
		_check_wake_on_inbound_tcp(server.name, vantage_name)


def _find_vantage(server_name: str) -> str | None:
	"""Another Active, SSH-reachable, REAL host to originate the inbound SYN from.

	Fake-provider Servers are excluded and the exclusion is load-bearing: a Task
	on a Fake host is synthesized in-process and never touches SSH, so a probe
	"succeeds" with canned output having dialed nothing. Picking one made the
	whole scenario pass vacuously — ICMP "didn't wake" the VM because no ping was
	ever sent, and TCP "woke" it without a packet leaving the controller.
	"""
	from atlas.atlas.providers.fake_tasks import is_fake_server
	from atlas.tests.e2e._shared import server_is_reachable

	for name in frappe.get_all("Server", filters={"status": "Active"}, pluck="name"):
		if name == server_name or is_fake_server(name):
			continue
		if server_is_reachable(name, timeout_seconds=5):
			return name
	return None


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


def _check_wake_on_inbound_tcp(server_name: str, vantage_name: str) -> None:
	"""The tenant-visible path: a sleeping VM wakes on the first inbound TCP SYN.

	Both stimuli are driven from `vantage_name`, a DIFFERENT host, because a packet
	originating on the VM's own host is input-delivered locally and never traverses
	`inet atlas forward` — the trap would never fire and the probe would prove
	nothing. The controller is not a usable vantage either: a laptop has no v6
	route to a guest (the same reason the migration use case probes from the
	target host).

	Runs on a fresh VM — _check_sleep_wake terminates its own.
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

	try:
		wait_for_vm_running(vm.name, timeout_seconds=180)
		vm.reload()
		address = vm.ipv6_address
		assert address, "VM has no public /128 to dial"

		# Sanity: the vantage must reach the guest while it is UP, or every
		# assertion below would pass or fail for reasons that have nothing to do
		# with the trap. This doubles as the wait for the guest to finish booting
		# — sleeping a VM whose Firecracker API socket is not yet listening makes
		# the memory snapshot fail ("API socket missing"), and the VM then wakes by
		# cold boot instead of resume, which is not what this scenario is testing.
		assert_probe(
			vantage_name,
			"phase-wake-tcp",
			timeout_seconds=180,
			TARGET_IPV6=address,
			TIMEOUT_SECONDS="150",
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

		# --- 2. ICMP must NOT wake it (the rule matches `tcp flags`) ---
		assert_probe(vantage_name, "phase-wake-ping", timeout_seconds=60, TARGET_IPV6=address)
		time.sleep(5)
		assert_probe(server_name, "phase7-is-sleeping", VIRTUAL_MACHINE_NAME=vm.name)
		vm.reload()
		assert vm.status == "Sleeping", f"ICMP woke the VM; the trap is not TCP-only ({vm.status})"

		# --- 3. A TCP SYN DOES wake it ---
		assert_probe(
			vantage_name,
			"phase-wake-tcp",
			timeout_seconds=200,
			TARGET_IPV6=address,
			TIMEOUT_SECONDS="150",
		)
		assert_probe(
			server_name, "phase5-is-active", timeout_seconds=90, VIRTUAL_MACHINE_NAME=vm.name
		)
		assert_probe(
			server_name,
			"phase-is-unparked",
			timeout_seconds=60,
			VIRTUAL_MACHINE_NAME=vm.name,
			VIRTUAL_MACHINE_IPV6=address,
		)

		# --- 4. The DB catches up. The host woke the VM on its own; Atlas learns
		#        it from the per-minute reconcile, not from a Task. ---
		reconcile_sleeping_vms()
		vm.reload()
		assert vm.status == "Running", f"reconcile did not adopt the host wake ({vm.status})"
		assert not vm.has_memory_snapshot, "the wake consumed the memory snapshot"
		assert vm.last_traffic_at, "adopting the wake must reset the idle clock"
	finally:
		vm.reload()
		if vm.status != "Terminated":
			vm.terminate()
