#!/usr/bin/env python3
# Upload a snapshot's on-host artifacts to S3 via controller-presigned PUT URLs.
# The host holds NO S3 credentials — it streams each object with curl to a
# short-lived URL the controller signed. Cold snapshots upload their disk LV(s);
# warm snapshots also upload the frozen memory pair + host signature.
#
# Each object is handled ONE AT A TIME: zstd-compress the source (an LV read
# directly, or a memory file) to a temp file — an S3 PUT needs a known length, so
# we can't stream unknown-length — sha256 it, curl -T it to the presigned URL,
# delete the temp. Peak temp space is therefore the largest single COMPRESSED
# object, not the sum. Idempotent: curl -T overwrites, so a re-run re-uploads.
#
# The Task contract is typed at both ends (UploadSnapshotInputs.from_args /
# UploadSnapshotResult.emit). See spec/29-snapshot-backup.md.

import os
import sys
import typing
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from atlas._run import install_directory, run
from atlas._task import TaskInputs, TaskResult
from atlas.lvm import ThinPool
from atlas.paths import ATLAS_ROOT
from atlas.snapshot_backup import BackupObject, parse_objects

# Backups favour a fast, well-parallelised compress over the last few percent of
# ratio: level 3 (zstd's default) with every core.
ZSTD_LEVEL = 3


@dataclass(frozen=True)
class UploadSnapshotInputs(TaskInputs):
	"""Upload a snapshot's artifacts to S3 with presigned PUT URLs."""

	command: typing.ClassVar[str] = "upload-snapshot-s3"
	snapshot_name: str  # names the temp working directory
	# JSON list; each item is a BackupObject dict plus its presigned `url`.
	objects_json: str


@dataclass(frozen=True)
class UploadSnapshotResult(TaskResult):
	# One dict per object: {name, object_name, sha256, compressed_bytes, raw_bytes}.
	objects: list
	total_compressed_bytes: int


def main() -> None:
	inputs = UploadSnapshotInputs.from_args()
	objects = parse_objects(inputs.objects_json)
	pool = ThinPool()

	work = f"{ATLAS_ROOT}/tmp/s3-upload-{inputs.snapshot_name}"
	run("sudo rm -rf {}", work)
	install_directory(work, mode="0700")
	try:
		uploaded = [_upload_one(pool, obj, work) for obj in objects]
	finally:
		run("sudo rm -rf {}", work)

	total = sum(item["compressed_bytes"] for item in uploaded)
	UploadSnapshotResult(objects=uploaded, total_compressed_bytes=total).emit()
	print(
		f"Uploaded {len(uploaded)} object(s) for snapshot {inputs.snapshot_name} ({total} compressed bytes)."
	)


def _upload_one(pool: ThinPool, obj: BackupObject, work: str) -> dict:
	"""Compress one artifact to a temp file, sha256 it, PUT it, delete the temp."""
	temp = f"{work}/{obj.object_name}"
	raw_bytes = _compress(pool, obj, temp)
	digest = run("sudo sha256sum {}", temp).split()[0]
	compressed_bytes = int(run("sudo stat -c %s {}", temp).strip())
	run("sudo curl --fail --silent --show-error --upload-file {} {}", temp, obj.url)
	run("sudo rm -f {}", temp)
	return {
		"name": obj.name,
		"object_name": obj.object_name,
		"sha256": digest,
		"compressed_bytes": compressed_bytes,
		"raw_bytes": raw_bytes,
	}


def _compress(pool: ThinPool, obj: BackupObject, temp: str) -> int:
	"""Write the compressed (or, for the tiny signature JSON, verbatim) artifact to
	`temp`. Returns the raw source size. zstd reads the LV block device (or file)
	directly — no dd, no pipe, so the exit code is honestly zstd's own."""
	source = _activated_source(pool, obj)
	raw_bytes = int(run("sudo blockdev --getsize64 {}", source).strip()) if obj.block else _file_size(source)
	if obj.compress:
		run("sudo zstd -q -f -{} -T0 -o {} {}", ZSTD_LEVEL, temp, source)
	else:
		run("sudo cp {} {}", source, temp)
	return raw_bytes


def _activated_source(pool: ThinPool, obj: BackupObject) -> str:
	"""The readable source path: a plain file as-is, or an activated LV device."""
	if not obj.block:
		if not os.path.isfile(obj.source):
			sys.exit(f"source file missing: {obj.source}")
		return obj.source
	lv = pool.from_device(obj.source)
	if not lv.exists:
		sys.exit(f"source LV not found: {obj.source} ({lv.name})")
	return lv.activate().device_path


def _file_size(path: str) -> int:
	return int(run("sudo stat -c %s {}", path).strip())


if __name__ == "__main__":
	main()
