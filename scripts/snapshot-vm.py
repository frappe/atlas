#!/usr/bin/env python3
# Snapshot a Stopped VM's disk: take an LVM thin CoW snapshot of its disk LV.
# Disk-only — no Firecracker memory-state snapshot. The caller guarantees the VM
# is Stopped, so the disk is cleanly unmounted and the snapshot is consistent.
# Instant and O(1): the snapshot shares the VM disk's blocks until one side is
# written. Pure host op — no jail interaction. Idempotent: re-running reuses the
# existing snapshot LV.
#
# Successor to snapshot-vm.sh. The Task contract is now typed at both ends:
# SnapshotInputs.from_args() parses CLI flags once; SnapshotResult.emit() writes
# one machine-readable line the controller parses back to a typed object — no env
# soup, no SIZE_BYTES= grepping. Invoked as a CLI:
#   snapshot-vm.py --virtual-machine-name <uuid> --snapshot-rootfs-path /dev/atlas/...
# which is exactly the subcommand shape a future `atlas` CLI mounts directly.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._task import TaskInputs, TaskResult
from atlas.lvm import ThinPool


@dataclass(frozen=True)
class SnapshotInputs(TaskInputs):
	"""Snapshot a Stopped VM's disk via an LVM thin CoW snapshot."""

	command: typing.ClassVar[str] = "snapshot-vm"
	virtual_machine_name: str  # UUID; identifies the source disk LV
	snapshot_rootfs_path: str  # the snapshot's /dev/atlas/<name> device path
	# The data-disk snapshot device path (atlas-datasnap-<id>). Empty when the VM
	# has no data disk — then only the root disk is snapshotted.
	data_snapshot_rootfs_path: str = ""


@dataclass(frozen=True)
class SnapshotResult(TaskResult):
	size_bytes: int
	data_size_bytes: int = 0  # 0 when the VM had no data disk


def main() -> None:
	inputs = SnapshotInputs.from_args()
	pool = ThinPool()

	disk = pool.vm_disk(inputs.virtual_machine_name)
	snapshot = pool.from_device(inputs.snapshot_rootfs_path)

	if not disk.exists:
		sys.exit(f"disk LV not found for {inputs.virtual_machine_name} ({disk.name}); provision the VM first")
	if pool.usage.too_full_to_snapshot:
		sys.exit(f"thin pool {pool.pool_name} too full for a safe snapshot ({pool.usage})")

	disk.snapshot_into(snapshot)

	# Snapshot the data disk too (the root disk's peer) when the VM has one. Same
	# instant CoW thin snapshot; the pair shares the snapshot's UUID so the
	# controller can name and later remove both. A missing data disk is tolerated
	# (the row claimed one but the LV is gone) — root is still captured.
	data_size_bytes = 0
	if inputs.data_snapshot_rootfs_path:
		data_disk = pool.data_disk(inputs.virtual_machine_name)
		if data_disk.exists:
			data_snapshot = pool.from_device(inputs.data_snapshot_rootfs_path)
			data_disk.snapshot_into(data_snapshot)
			data_size_bytes = data_snapshot.size_bytes

	SnapshotResult(size_bytes=snapshot.size_bytes, data_size_bytes=data_size_bytes).emit()
	print(f"Snapshotted {inputs.virtual_machine_name} to {snapshot.name}.")


if __name__ == "__main__":
	main()
