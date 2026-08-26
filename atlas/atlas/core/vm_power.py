"""The VM power state-machine verbs that run over Boat's desired-state
transport: start, sleep, wake, restart, pause, resume.

Extracted from the `VirtualMachine` controller — driving a guest between power
states is one cohesive responsibility, separate from the doc's
validation/defaults and from the desired-state plumbing (`_transport` /
`_put_desired_state`) these verbs dispatch through, which stays on the
controller. Each verb states intent on the host's Boat first (via
`vm._transport`, which returns `run_boat_task` bound in the controller's
namespace, so the test mock seam is preserved) and then runs the matching Task.

`stop` is deliberately NOT here: it is the one verb Boat serves no equivalent
of — its memory-snapshot path keeps its own SSH `run_task` transport — and it
is the migration cold-stop entry, so it stays with the controller's transport
plumbing. The split here is by transport, not by name.

Free functions taking the `VirtualMachine`, following the vm_provisioning.py /
vm_images.py pattern; the controller keeps thin `@frappe.whitelist()`
delegators so Desk's `frm.call()` and the RPC surface are unchanged.
"""

from __future__ import annotations

import frappe
from frappe import _

from atlas.atlas.core.networking import derive_uid
from atlas.atlas.core.task_results import parse_optional_result


def start(vm, correlation_id: str | None = None) -> str:
	"""Start a Stopped VM. When the last stop captured a memory snapshot
	(has_memory_snapshot), the host resumes the guest from it in milliseconds
	instead of cold-booting; the start Task is the same either way — the
	launcher and the unit's vm-restore.py hook decide from the on-host marker.
	The snapshot is consumed by the start (restored or not), so the flag
	clears here unconditionally.

	A Sleeping VM is woken instead of started — Desk's Start button works for
	both states transparently."""
	if vm.status == "Sleeping":
		return vm.wake()
	if vm.status != "Stopped":
		frappe.throw(f"Cannot start from {vm.status}")
	vm._guard_no_active_migration()
	run = vm._transport("Running")
	task = run(
		server=vm.server,
		script="start-vm",
		variables={"VIRTUAL_MACHINE_NAME": vm.name},
		virtual_machine=vm.name,
		timeout_seconds=30,
	)
	vm.status = "Running"
	vm.has_memory_snapshot = 0
	vm.last_started = frappe.utils.now_datetime()
	# Treat the start itself as activity (spec/32). Without this a sleep_on_idle
	# VM carries the last_traffic_at it had before it stopped — already older
	# than idle_timeout_seconds — and the next sleep_idle_vms tick puts it
	# straight back to sleep, within a minute of the operator starting it.
	vm.last_traffic_at = vm.last_started
	vm.correlation_id = correlation_id
	vm.save()
	return task.name


def sleep(vm) -> str:
	"""Put a Running VM to sleep: memory snapshot on the host + SLEEPING marker
	file that suppresses systemd auto-start on host reboot. The VM's cgroup is
	released, freeing its RAM on the host — that is the whole point.

	Falls back to a plain stop if the snapshot fails (launcher too old, not
	enough disk, etc.) — the VM always ends up Sleeping; only the next wake's
	speed differs. sleep_on_idle must be enabled on the VM."""
	if not vm.sleep_on_idle:
		frappe.throw(_("Enable sleep_on_idle before putting this VM to sleep"))
	if vm.status != "Running":
		frappe.throw(f"Cannot sleep from {vm.status}")
	vm._guard_no_active_migration()
	if vm.stop_protection:
		frappe.throw(_("Disable stop protection before sleeping this VM"))
	if vm.desired_power == "Stopped":
		# The precedence rule from the enrolment side (spec/33 §11.3). Sleeping
		# is a Running VM's low-power state — the address stays live and the
		# first SYN brings it back — so parking a VM that was told to stop
		# would arm exactly the resurrection the rule forbids.
		#
		# This read used to be gated on `boat_enabled`, because a VM left
		# stating Stopped on a rolled-back host could never sleep again —
		# silently, since the idle sweeper swallows this throw. With the flag
		# gone the field is authoritative everywhere and the guard is simply
		# the rule.
		frappe.throw(_("VM is stopped by intent — start it before putting it to sleep"))
	# Sleeping satisfies Running: the VM is parked and wakeable, not powered
	# off, so the intent it is parked under stays Running.
	run = vm._transport("Running")
	task = run(
		server=vm.server,
		script="sleep-vm",
		variables={
			"VIRTUAL_MACHINE_NAME": vm.name,
			"ATLAS_FC_UID": str(derive_uid(vm.name)),
		},
		virtual_machine=vm.name,
		timeout_seconds=120,
	)
	# The VM is parked either way, so the row reads Sleeping either way. Whether
	# the host also dumped its RAM only changes the next wake's SPEED, and it is
	# the one thing this verb cannot always learn: Boat computes it and the
	# contract has nowhere to put it yet (`boat_client.OPERATION_RESULT_FIELD`),
	# so on a Boat host the Task carries no result line at all. Insisting on one
	# is how an idle VM ended up parked on its host and still Running in the DB —
	# with the throw swallowed by the idle sweeper, which then re-slept it once a
	# minute, forever, each time with a fresh op_id Boat genuinely re-ran.
	#
	# `has_memory_snapshot` is bookkeeping, not authority (spec/02): the on-host
	# READY marker decides at wake time, so leaving it untouched when the
	# transport did not say costs the operator a display detail and nothing else.
	result = parse_optional_result(task.stdout)
	vm.status = "Sleeping"
	if result is not None:
		vm.has_memory_snapshot = 1 if result.get("memory_snapshot") else 0
	vm.last_stopped = frappe.utils.now_datetime()
	vm.save()
	return task.name


def wake(vm) -> str:
	"""Wake a Sleeping VM. Removes the SLEEPING marker on the host so systemd
	will auto-start it on the next host reboot, then starts the unit. If a
	memory snapshot is present (has_memory_snapshot), the guest resumes in
	milliseconds; otherwise it cold-boots."""
	if vm.status != "Sleeping":
		frappe.throw(f"Cannot wake from {vm.status}")
	# FOR UPDATE holds the row lock for this transaction, preventing two
	# concurrent wake() calls (e.g. two proxy wake-ups) from both dispatching
	# a start Task and racing each other.
	frappe.db.sql("SELECT name FROM `tabVirtual Machine` WHERE name = %s FOR UPDATE", vm.name)
	current_status = frappe.db.get_value("Virtual Machine", vm.name, "status")
	if current_status != "Sleeping":
		return ""  # Another caller already woke it
	# An operator wake states Running, exactly as start() does — this is the
	# explicit reversal of an intent, which is the one thing allowed to
	# outrank a Stopped. What must not resurrect a stopped VM is *traffic*:
	# that path is `_adopt_wake`, and it refuses (spec/33 §11.3).
	run = vm._transport("Running")
	task = run(
		server=vm.server,
		script="wake-vm",
		variables={"VIRTUAL_MACHINE_NAME": vm.name},
		virtual_machine=vm.name,
		timeout_seconds=30,
	)
	vm.status = "Running"
	vm.has_memory_snapshot = 0
	vm.last_started = frappe.utils.now_datetime()
	# The wake is itself the activity (spec/32) — same reason as start(), and
	# more acute here: this VM slept *because* last_traffic_at was stale, so
	# leaving it would guarantee the next idle sweep re-sleeps it. _adopt_wake
	# stamps the same field for the host-initiated (packet-triggered) wake.
	vm.last_traffic_at = vm.last_started
	vm.save()
	return task.name


def restart(vm, cold: bool = False) -> dict:
	"""Stop (if Running) then Start. Two Tasks. A Paused VM must resume or
	stop first — restart is deliberately Running/Stopped only.

	When the VM opted into memory_snapshot_on_stop, a restart is a
	state-preserving POWER CYCLE: the stop captures the guest's memory and
	the start resumes it — milliseconds, but the guest never reboots, so a
	wedged guest stays wedged. Pass `cold=True` for a true reboot (plain
	stop, full cold boot). Without the opt-in, restart is the plain
	stop + cold boot it always was."""
	if vm.status not in ("Running", "Stopped"):
		frappe.throw(f"Cannot restart from {vm.status}")
	cold = cold in (True, 1, "1", "true", "True", "yes")
	stop_task = vm.stop(memory_snapshot=False if cold else None) if vm.status == "Running" else None
	start_task = vm.start()
	return {"stop_task": stop_task, "start_task": start_task}


def pause(vm) -> str:
	"""Freeze a Running VM's vCPUs via Firecracker's API socket. RAM stays
	resident (unlike Stop, which is a full shutdown). Reversible with
	resume()."""
	if vm.status != "Running":
		frappe.throw(f"Cannot pause from {vm.status}")
	vm._guard_no_active_migration()
	# A paused VM's unit is still active and its RAM still resident, so the
	# intent stays Running: Stopped here would have the reconciler shut down
	# the machine the operator only meant to freeze.
	run = vm._transport("Running")
	task = run(
		server=vm.server,
		script="pause-vm",
		variables={"VIRTUAL_MACHINE_NAME": vm.name},
		virtual_machine=vm.name,
		timeout_seconds=30,
	)
	vm.status = "Paused"
	vm.save()
	return task.name


def resume(vm) -> str:
	"""Unfreeze a Paused VM's vCPUs via the API socket."""
	if vm.status != "Paused":
		frappe.throw(f"Cannot resume from {vm.status}")
	vm._guard_no_active_migration()
	run = vm._transport("Running")
	task = run(
		server=vm.server,
		script="resume-vm",
		variables={"VIRTUAL_MACHINE_NAME": vm.name},
		virtual_machine=vm.name,
		timeout_seconds=30,
	)
	vm.status = "Running"
	vm.last_started = frappe.utils.now_datetime()
	vm.save()
	return task.name
