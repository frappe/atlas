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


@dataclass(frozen=True)
class SnapshotResult(TaskResult):
    size_bytes: int


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

    SnapshotResult(size_bytes=snapshot.size_bytes).emit()
    print(f"Snapshotted {inputs.virtual_machine_name} to {snapshot.name}.")


if __name__ == "__main__":
    main()
