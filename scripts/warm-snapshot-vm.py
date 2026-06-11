#!/usr/bin/env python3
# Capture a WARM golden snapshot of a Running (pre-warmed) VM: the guest's full
# memory state AND an LVM thin snapshot of its disk, both at one paused instant,
# written to a durable artifact location. The fan-out producer: N future clones
# each restore this pair instead of cold-booting (~17s → low seconds).
#
#   1. Pause the vCPUs over the API socket.
#   2. PUT /snapshot/create — vmstate + RAM file land in the jail (the jailed
#      Firecracker can only write inside its chroot).
#   3. LVM thin snapshot of the disk LV — SAME paused instant, so the frozen
#      RAM's filesystem cache matches the captured disk exactly (the pair is
#      only valid together).
#   4. Move the pair to the durable per-snapshot directory (same filesystem —
#      a rename), world-readable so any per-VM uid's jailed Firecracker can map
#      it, and record the host signature (CPU/kernel/FC) beside it for
#      vm-restore.py's compatibility guard.
#   5. Resume the VM. No READY marker is ever written in the source jail — the
#      golden VM itself must never resume from this pair.
#
# Unlike snapshot-stop-vm.py (the opt-in fast stop, which falls back to a plain
# stop), this is an operator bake step: any failure is loud and fails the Task.

import json
import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import CommandError, firecracker_api, install_directory, install_file, run, run_ok
from atlas._task import TaskInputs, TaskResult
from atlas.hostinfo import host_signature
from atlas.lvm import ThinPool
from atlas.paths import ATLAS_ROOT, SNAPSHOTS_DIRECTORY, VirtualMachinePaths

# Headroom beyond the worst-case memory file so a capture can never wedge the
# host filesystem against full (same margin as snapshot-stop-vm.py).
FREE_SPACE_MARGIN_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class WarmSnapshotInputs(TaskInputs):
	"""Capture a warm golden snapshot (memory + disk at one paused instant) of a
	Running VM into a durable artifact directory. Fails loud — this is a bake
	step, not an opportunistic fast path."""

	command: typing.ClassVar[str] = "warm-snapshot-vm"
	virtual_machine_name: str  # UUID; selects the unit, jail and API socket
	atlas_fc_uid: int  # per-VM uid; the jailed Firecracker writes the pair
	snapshot_rootfs_path: str  # the disk snapshot's /dev/atlas/<name> device path
	memory_directory: str  # durable directory for vmstate.bin/mem.bin/host-signature.json


@dataclass(frozen=True)
class WarmSnapshotResult(TaskResult):
	size_bytes: int  # disk snapshot LV size
	memory_bytes: int  # on-disk size of the captured memory file
	host_signature: str  # JSON dict: cpu model/flags/microcode, kernel, FC version


def main() -> None:
	inputs = WarmSnapshotInputs.from_args()
	paths = VirtualMachinePaths(inputs.virtual_machine_name)
	pool = ThinPool()

	if not inputs.memory_directory.startswith(SNAPSHOTS_DIRECTORY + "/"):
		sys.exit(f"memory directory must live under {SNAPSHOTS_DIRECTORY}: {inputs.memory_directory}")
	_preflight(inputs, paths, pool)

	snapshot = pool.from_device(inputs.snapshot_rootfs_path)
	firecracker_api(paths.api_socket_directory, paths.api_socket_name, "PATCH", "/vm", '{"state": "Paused"}')
	try:
		_create_memory_pair(paths, inputs.atlas_fc_uid)
		# Disk snapshot at the SAME paused instant — the frozen RAM references
		# exactly these blocks.
		pool.vm_disk(inputs.virtual_machine_name).snapshot_into(snapshot)
	finally:
		firecracker_api(
			paths.api_socket_directory, paths.api_socket_name, "PATCH", "/vm", '{"state": "Resumed"}'
		)

	signature = host_signature()
	_stage_durable(paths, inputs.memory_directory, signature)

	mem_bytes = int(run("sudo", "stat", "-c", "%s", f"{inputs.memory_directory}/mem.bin").strip())
	WarmSnapshotResult(
		size_bytes=snapshot.size_bytes,
		memory_bytes=mem_bytes,
		host_signature=json.dumps(signature),
	).emit()
	print(f"Captured warm snapshot of {inputs.virtual_machine_name} into {inputs.memory_directory}.")


def _preflight(inputs: "WarmSnapshotInputs", paths: VirtualMachinePaths, pool: ThinPool) -> None:
	if not os.path.exists(paths.api_socket):
		sys.exit("API socket missing; is the VM running?")
	if pool.data_disk(inputs.virtual_machine_name).exists:
		sys.exit("warm snapshots do not support a data disk; bake from a VM without one")
	if pool.usage.too_full_to_snapshot:
		sys.exit(f"thin pool {pool.pool_name} too full for a safe snapshot ({pool.usage})")
	# The memory file is RAM-sized; require the worst case plus margin free.
	run("sudo", "rm", "-rf", paths.memory_snapshot_directory)
	mem_size_mib = int(
		run("sudo", "jq", "-r", '."machine-config".mem_size_mib', paths.firecracker_config).strip()
	)
	needed = mem_size_mib * 1024 * 1024 + FREE_SPACE_MARGIN_BYTES
	available = int(run("df", "--output=avail", "-B1", ATLAS_ROOT).splitlines()[1].strip())
	if available < needed:
		sys.exit(f"not enough free space for a {mem_size_mib} MiB memory file ({available} B available)")


def _create_memory_pair(paths: VirtualMachinePaths, uid: int) -> None:
	"""PUT /snapshot/create into the jail (the only place the jailed Firecracker
	can write), then verify both files landed non-empty."""
	install_directory(paths.memory_snapshot_directory, mode="0700")
	run("sudo", "chown", f"{uid}:{uid}", paths.memory_snapshot_directory)
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
		if not run_ok("sudo", "test", "-s", snapshot_file):
			raise CommandError(["test", "-s", snapshot_file], 1, "snapshot file missing or empty")


def _stage_durable(paths: VirtualMachinePaths, memory_directory: str, signature: dict) -> None:
	"""Move the pair out of the jail to the durable directory (same filesystem —
	an instant rename) and record the host signature beside it. 0644 inodes: the
	files are later hard-linked into clone jails and mapped read-only by jailed
	Firecrackers running under arbitrary per-VM uids (MAP_PRIVATE never writes
	back). The source jail's snapshot directory is removed — no marker was ever
	written, so the golden VM can never resume from it."""
	install_directory(memory_directory, mode="0755")
	for name in ("vmstate.bin", "mem.bin"):
		run("sudo", "mv", f"{paths.memory_snapshot_directory}/{name}", f"{memory_directory}/{name}")
		run("sudo", "chown", "root:root", f"{memory_directory}/{name}")
		run("sudo", "chmod", "0644", f"{memory_directory}/{name}")
	install_file(json.dumps(signature, indent=1) + "\n", f"{memory_directory}/host-signature.json")
	run("sudo", "rm", "-rf", paths.memory_snapshot_directory)


if __name__ == "__main__":
	main()
