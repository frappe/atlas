#!/usr/bin/env python3
# Rehydrate a snapshot's on-host artifacts from S3 via controller-presigned GET
# URLs. The host holds NO S3 credentials — it pulls each object with curl from a
# short-lived URL. This is the inverse of upload-snapshot-s3.py: recreate the disk
# LV(s) the snapshot row already names (atlas-snap-<id>, atlas-datasnap-<id>) and,
# for a warm snapshot, the memory pair + host signature under the snapshot's
# memory directory. After this the snapshot is fully local again, so the ordinary
# restore_to_vm / clone_to_new_vm paths work unchanged.
#
# Each object: curl the compressed bytes to a temp file, verify its sha256 (before
# decompressing — the sync-image integrity gate), then zstd-decompress straight
# onto the recreated LV (--sparse, so a fresh thin LV stays thin) or into the
# memory file. Idempotent: a block LV is removed and recreated clean each time.
# See spec/29-snapshot-backup.md.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import install_directory, run, run_input
from atlas._task import TaskInputs, TaskResult
from atlas.lvm import ThinPool
from atlas.paths import ATLAS_ROOT
from atlas.snapshot_backup import BackupObject, parse_objects


@dataclass(frozen=True)
class RestoreSnapshotInputs(TaskInputs):
	"""Rehydrate a snapshot's artifacts from S3 with presigned GET URLs."""

	command: typing.ClassVar[str] = "restore-snapshot-s3"
	snapshot_name: str  # names the temp working directory
	# JSON list; each item is a BackupObject dict plus its presigned `url` + `sha256`.
	objects_json: str


@dataclass(frozen=True)
class RestoreSnapshotResult(TaskResult):
	objects: list  # the names rehydrated, e.g. ["rootfs", "data"]


def main() -> None:
	inputs = RestoreSnapshotInputs.from_args()
	objects = parse_objects(inputs.objects_json)
	pool = ThinPool()

	if any(obj.block for obj in objects) and pool.usage.too_full_to_snapshot:
		sys.exit(f"thin pool {pool.pool_name} too full to restore into ({pool.usage})")

	work = f"{ATLAS_ROOT}/tmp/s3-restore-{inputs.snapshot_name}"
	run("sudo rm -rf {}", work)
	install_directory(work, mode="0700")
	try:
		for obj in objects:
			_restore_one(pool, obj, work)
	finally:
		run("sudo rm -rf {}", work)
	run("sudo sync")

	RestoreSnapshotResult(objects=[obj.name for obj in objects]).emit()
	print(f"Rehydrated {len(objects)} object(s) for snapshot {inputs.snapshot_name}.")


def _restore_one(pool: ThinPool, obj: BackupObject, work: str) -> None:
	"""Download one object, verify its sha256, and write it to its destination."""
	temp = f"{work}/{obj.object_name}"
	run("sudo curl --fail --silent --show-error --output {} {}", temp, obj.url)
	run_input("sudo sha256sum -c -", stdin=f"{obj.sha256}  {temp}")
	if obj.block:
		_restore_block(pool, obj, temp)
	else:
		_restore_file(obj, temp)
	run("sudo rm -f {}", temp)


def _restore_block(pool: ThinPool, obj: BackupObject, temp: str) -> None:
	"""Recreate the LV clean (so a re-restore has no stale blocks), then decompress
	the image straight onto it. --sparse skips zero runs, keeping the thin LV thin."""
	if not obj.disk_gigabytes:
		sys.exit(f"block object {obj.name} has no disk size to recreate {obj.source}")
	lv = pool.from_device(obj.source)
	lv.remove()  # no-op if absent; refuses protected LVs (atlas-image-*)
	pool.create_thin(lv, obj.disk_gigabytes)
	run("sudo zstd -d -q -f --sparse -o {} {}", lv.device_path, temp)


def _restore_file(obj: BackupObject, temp: str) -> None:
	"""Write a warm memory-pair file (root:root 0644, like warm-snapshot-vm.py's
	_stage_durable) into the recreated memory directory."""
	install_directory(os.path.dirname(obj.source), mode="0755")
	if obj.compress:
		run("sudo zstd -d -q -f -o {} {}", obj.source, temp)
	else:
		run("sudo cp {} {}", temp, obj.source)
	run("sudo chown root:root {}", obj.source)
	run("sudo chmod 0644 {}", obj.source)


if __name__ == "__main__":
	main()
