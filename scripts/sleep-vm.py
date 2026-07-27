#!/usr/bin/env python3
# Put a Running VM to sleep: capture its full memory state, stop the unit, then
# write a SLEEPING marker file. The marker prevents systemd from auto-starting the
# VM on host reboot (ConditionPathNotExists in firecracker-vm@.service) and is the
# authority for the Sleeping status in Frappe.
#
# Falls back to plain stop on any snapshot failure — the VM always ends up stopped;
# only the next wake's speed differs. The SLEEPING marker is written after the unit
# stops in both paths, so the reboot-suppression always takes effect.
#
# Mirrors snapshot-stop-vm.py's snapshot logic exactly; they share no library
# because the jail/paths setup is identical and a shared lib would add indirection
# with no reuse benefit (two callers, same repo, always evolved together).

import json
import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import CommandError, firecracker_api, install_directory, run, run_ok
from atlas._task import TaskInputs, TaskResult
from atlas.park import park
from atlas.paths import ATLAS_ROOT, VirtualMachinePaths

FREE_SPACE_MARGIN_BYTES = 256 * 1024 * 1024
WAKE_TRAP_UNIT = "atlas-wake-trap.service"


@dataclass(frozen=True)
class SleepVmInputs(TaskInputs):
	"""Stop a VM with a memory snapshot and mark it sleeping (no reboot auto-start)."""

	command: typing.ClassVar[str] = "sleep-vm"
	virtual_machine_name: str
	atlas_fc_uid: int


@dataclass(frozen=True)
class SleepVmResult(TaskResult):
	memory_snapshot: bool
	reason: str = ""
	memory_snapshot_bytes: int = 0


def main() -> None:
	inputs = SleepVmInputs.from_args()
	paths = VirtualMachinePaths(inputs.virtual_machine_name)

	_require_wake_trap()

	reason = _preflight(paths)
	if reason:
		_stop_and_sleep(paths, reason)
		return

	try:
		_create_snapshot(paths, inputs.atlas_fc_uid)
	except CommandError as error:
		_stop_and_sleep(paths, f"snapshot failed: {error}")
		return

	run("sudo systemctl stop {}", paths.systemd_unit)
	run("sudo touch {}", paths.sleeping_marker)
	_park_for_wake(paths.uuid)
	mem_bytes = int(run("sudo stat -c %s {}", paths.memory_snapshot_mem).strip())
	SleepVmResult(memory_snapshot=True, memory_snapshot_bytes=mem_bytes).emit()
	print(f"VM {inputs.virtual_machine_name} is now sleeping (memory snapshot captured).")


def _require_wake_trap() -> None:
	"""Refuse to sleep when nothing on this host could wake the VM again.

	atlas-wake-trap.service is what turns an inbound SYN into a `systemctl start`.
	Without it a slept VM is still parked — its /128 routes into the atlas-park0
	dummy — so it answers nothing and stays dark until an operator clicks Start.
	That is strictly worse than leaving the VM awake, and it fails SILENTLY: the
	sleep Task succeeds and the tenant just sees a black hole.

	This is not hypothetical. A host that had `sync-scripts` run but was never
	re-bootstrapped had atlas-wake-trap.py and park.py on disk with no unit file
	(units ship at bootstrap, not with a script sync); VMs slept there and could
	not be woken by traffic at all. Fail loudly and leave the VM Running instead —
	the Task row records why, and the fix is to bootstrap the server."""
	if not run_ok("systemctl is-active --quiet {}", WAKE_TRAP_UNIT):
		sys.exit(
			f"refusing to sleep: {WAKE_TRAP_UNIT} is not active on this host, so an "
			"inbound connection could not wake the VM. Bootstrap the server to install "
			"and enable it."
		)


def _preflight(paths: VirtualMachinePaths) -> str:
	"""Return a non-empty reason string if a memory snapshot can't be taken."""
	if not run_ok("sudo grep -q snapshot/READY {}", paths.jailer_launch):
		return "launcher predates memory snapshots; re-provision the VM to enable fast start"
	if not os.path.exists(paths.api_socket):
		return "API socket missing; is the VM running?"
	run("sudo rm -rf {}", paths.memory_snapshot_directory)
	mem_size_mib = int(
		run("sudo jq -r {} {}", '."machine-config".mem_size_mib', paths.firecracker_config).strip()
	)
	needed = mem_size_mib * 1024 * 1024 + FREE_SPACE_MARGIN_BYTES
	available = int(run("df --output=avail -B1 {}", ATLAS_ROOT).splitlines()[1].strip())
	if available < needed:
		return f"not enough free space for a {mem_size_mib} MiB memory file ({available} B available)"
	return ""


def _create_snapshot(paths: VirtualMachinePaths, uid: int) -> None:
	install_directory(paths.memory_snapshot_directory, mode="0700")
	run("sudo chown {} {}", f"{uid}:{uid}", paths.memory_snapshot_directory)
	firecracker_api(paths.api_socket_directory, paths.api_socket_name, "PATCH", "/vm", '{"state": "Paused"}')
	firecracker_api(
		paths.api_socket_directory,
		paths.api_socket_name,
		"PUT",
		"/snapshot/create",
		json.dumps(
			{
				"snapshot_type": "Full",
				"snapshot_path": paths.memory_snapshot_vmstate_in_jail,
				"mem_file_path": paths.memory_snapshot_mem_in_jail,
			}
		),
	)
	for snapshot_file in (paths.memory_snapshot_vmstate, paths.memory_snapshot_mem):
		if not run_ok("sudo test -s {}", snapshot_file):
			raise CommandError(["test", "-s", snapshot_file], 1, "snapshot file missing or empty")
	run("sudo touch {}", paths.memory_snapshot_marker)


def _stop_and_sleep(paths: VirtualMachinePaths, reason: str) -> None:
	"""Fallback path: no memory snapshot, but still write the SLEEPING marker so
	the unit won't auto-restart on host reboot. The stale snapshot marker (if any)
	must not survive a partial run."""
	run("sudo rm -f {}", paths.memory_snapshot_marker)
	run("sudo systemctl stop {}", paths.systemd_unit)
	run("sudo touch {}", paths.sleeping_marker)
	_park_for_wake(paths.uuid)
	SleepVmResult(memory_snapshot=False, reason=reason).emit()
	print(f"VM {paths.uuid} is now sleeping (no memory snapshot): {reason}")


def _park_for_wake(uuid: str) -> None:
	"""Install the parked reachability + TCP-SYN wake trap (atlas.park) so an inbound
	TCP connection wakes the VM (spec/32). Done AFTER the marker is written, so the
	VM is already officially sleeping.

	A failure here is NOT swallowed. The VM is stopped and marked sleeping by this
	point, so we cannot undo it — but reporting success would tell the controller a
	VM is safely asleep when nothing can wake it, the same silent black hole
	_require_wake_trap() exists to prevent. Exit non-zero so the Task records the
	failure and an operator sees it; the VM is still wakeable via Start, and
	atlas-wake-trap re-parks every marked VM at its next startup."""
	try:
		park(uuid)
	except Exception as error:
		sys.exit(
			f"VM is stopped and marked sleeping, but parking the wake trap FAILED: {error}. "
			"It will not wake on inbound traffic until re-parked (restart "
			f"{WAKE_TRAP_UNIT} to re-park, or Start the VM)."
		)


if __name__ == "__main__":
	main()
