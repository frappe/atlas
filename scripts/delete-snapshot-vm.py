#!/usr/bin/env python3
# Delete a VM disk snapshot. Idempotent: a missing LV is a no-op.
# Run from Virtual Machine Snapshot.on_trash when the row is deleted.
#
# Successor to delete-snapshot-vm.sh. Pure host op — no jail interaction. The
# snapshot's /dev/atlas/<name> device path is the only input; its basename is the
# snapshot LV to remove. Invoked as a CLI:
#   delete-snapshot-vm.py --snapshot-rootfs-path /dev/atlas/atlas-snap-<uuid>

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._task import TaskInputs
from atlas.lvm import ThinPool


@dataclass(frozen=True)
class DeleteSnapshotInputs(TaskInputs):
	"""Delete a VM disk snapshot LV by its device path."""

	command: typing.ClassVar[str] = "delete-snapshot-vm"
	snapshot_rootfs_path: str  # the snapshot's /dev/atlas/<name> device path
	# The data-disk snapshot device path (atlas-datasnap-<id>). Empty when the
	# snapshot captured no data disk.
	data_snapshot_rootfs_path: str = ""


def main() -> None:
	inputs = DeleteSnapshotInputs.from_args()
	pool = ThinPool()

	snapshot = pool.from_device(inputs.snapshot_rootfs_path)
	# remove() is guarded (refuses pool/image LVs) and a no-op if absent. A
	# snapshot LV is an independent thin volume — removing it never affects the VM
	# disk it was taken from, nor any clone made from it (clones are independent
	# thin LVs once created).
	snapshot.remove()

	# Remove the data-disk snapshot half too, when the snapshot had one. Same
	# guarded, idempotent remove.
	if inputs.data_snapshot_rootfs_path:
		pool.from_device(inputs.data_snapshot_rootfs_path).remove()

	print(f"Deleted snapshot {snapshot.name}.")


if __name__ == "__main__":
	main()
