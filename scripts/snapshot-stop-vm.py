#!/usr/bin/env python3
# Stop a VM, capturing its full memory state first so the next Start can resume
# it in milliseconds instead of cold-booting (60-120s to SSH). The fast-stop
# counterpart of vm-restore.py:
#
#   1. Pause the vCPUs over the API socket (idempotent; a Paused VM stays put).
#   2. PUT /snapshot/create — Firecracker writes the vmstate + a RAM-sized
#      memory file into the jail's snapshot/ directory.
#   3. Write the READY marker — ONLY once the pair is complete on disk. The
#      marker is the whole contract: the launcher and vm-restore.py key off it.
#   4. systemctl stop (the plain shutdown path, unchanged).
#
# Any pre-flight or snapshot failure falls back to the plain stop and reports
# memory_snapshot=false with the reason — the VM always ends up Stopped; only
# the next Start's speed differs. The fallback also clears any stale marker so
# a half-written snapshot can never be restored.
#
# The memory file is the guest's RAM size on disk. The snapshot lives inside
# the jail (terminate's rm -rf sweeps it; the per-VM uid owns it so the jailed
# Firecracker can write it) and is consumed by the next Start. NOTE: a restored
# guest continues exactly where it paused — its clock is stale until NTP
# corrects it, and it never observes a reboot.

import json
import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import CommandError, firecracker_api, install_directory, run, run_ok
from atlas._task import TaskInputs, TaskResult
from atlas.paths import ATLAS_ROOT, VirtualMachinePaths

# Headroom beyond the worst-case memory file so a snapshot can never wedge the
# host filesystem against full.
FREE_SPACE_MARGIN_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class SnapshotStopInputs(TaskInputs):
	"""Stop a VM, capturing a full memory snapshot first so the next Start
	resumes instead of cold-booting. Falls back to a plain stop on any failure."""

	command: typing.ClassVar[str] = "snapshot-stop-vm"
	virtual_machine_name: str  # UUID; selects the unit, jail and API socket
	atlas_fc_uid: int  # per-VM uid; the jailed Firecracker writes the snapshot


@dataclass(frozen=True)
class SnapshotStopResult(TaskResult):
	memory_snapshot: bool  # True iff the READY marker was written
	reason: str = ""  # why the snapshot was skipped (memory_snapshot False)
	memory_snapshot_bytes: int = 0  # on-disk size of the memory file


def main() -> None:
	inputs = SnapshotStopInputs.from_args()
	paths = VirtualMachinePaths(inputs.virtual_machine_name)

	# Pre-flights. Each miss takes the default path — the operator asked for a
	# stop, and a stop is what they get either way.
	reason = _preflight(paths)
	if reason:
		_plain_stop(paths, reason)
		return

	try:
		_create_snapshot(paths, inputs.atlas_fc_uid)
	except CommandError as error:
		_plain_stop(paths, f"snapshot failed: {error}")
		return

	run("sudo systemctl stop {}", paths.systemd_unit)
	mem_bytes = int(run("sudo stat -c %s {}", paths.memory_snapshot_mem).strip())
	SnapshotStopResult(memory_snapshot=True, memory_snapshot_bytes=mem_bytes).emit()
	print(f"Stopped {inputs.virtual_machine_name} with a memory snapshot.")


def _preflight(paths: VirtualMachinePaths) -> str:
	"""The reason a memory snapshot can't be taken, or "" to proceed."""
	# A launcher generated before this feature always passes --config-file, so a
	# marker would strand the next start (snapshot loaded over a booted guest is
	# refused). Re-provisioning regenerates the launcher.
	if not run_ok("sudo grep -q snapshot/READY {}", paths.jailer_launch):
		return "launcher predates memory snapshots; re-provision the VM to enable fast start"
	if not os.path.exists(paths.api_socket):
		return "API socket missing; is the VM running?"
	# The memory file is RAM-sized. Drop the previous snapshot first (its space
	# is reclaimed, and a stale marker must not survive a failure below), then
	# require the worst case plus margin to be free.
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
	# The jailed Firecracker (per-VM uid) writes the snapshot pair, so the
	# directory must exist inside the jail and be owned by that uid.
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
	# Belt and suspenders: the marker asserts a COMPLETE pair, so verify both
	# files landed non-empty before writing it.
	for snapshot_file in (paths.memory_snapshot_vmstate, paths.memory_snapshot_mem):
		if not run_ok("sudo test -s {}", snapshot_file):
			raise CommandError(["test", "-s", snapshot_file], 1, "snapshot file missing or empty")
	run("sudo touch {}", paths.memory_snapshot_marker)


def _plain_stop(paths: VirtualMachinePaths, reason: str) -> None:
	"""The default path: no marker may survive (a partial snapshot must never be
	restored), then the ordinary unit stop."""
	run("sudo rm -f {}", paths.memory_snapshot_marker)
	run("sudo systemctl stop {}", paths.systemd_unit)
	SnapshotStopResult(memory_snapshot=False, reason=reason).emit()
	print(f"Stopped {paths.uuid} without a memory snapshot: {reason}")


if __name__ == "__main__":
	main()
