"""Pure helpers shared by upload-snapshot-s3.py and restore-snapshot-s3.py.

The controller sends the object plan as a JSON `--objects-json` flag; this parses
it into typed `BackupObject`s. Pure (no host), so it unit-tests without LVM or
curl. The transport fields (the presigned `url`, and on restore the expected
`sha256`) ride in the same dict and are read straight off the object.
See spec/29-snapshot-backup.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class BackupObject:
	"""One artifact to move to/from S3.

	`source` is the on-host path — an LV device (`block=True`) or a plain file
	(the warm memory pair). On restore it is the *destination* (the same path the
	snapshot row records), so upload and restore share this shape. `disk_gigabytes`
	is the LV size to recreate on restore (0 for a file). `compress` is zstd for
	everything but the tiny host-signature JSON. `url` is the presigned PUT
	(upload) or GET (restore); `sha256` is the expected digest of the compressed
	bytes (restore only)."""

	name: str
	object_name: str
	source: str
	block: bool
	compress: bool
	disk_gigabytes: int = 0
	url: str = ""
	sha256: str = ""

	@classmethod
	def from_plan(cls, item: dict) -> "BackupObject":
		return cls(
			name=item["name"],
			object_name=item["object_name"],
			source=item["source"],
			block=bool(item["block"]),
			compress=bool(item["compress"]),
			disk_gigabytes=int(item.get("disk_gigabytes") or 0),
			url=item.get("url", ""),
			sha256=item.get("sha256", ""),
		)


def parse_objects(objects_json: str) -> list[BackupObject]:
	"""Parse the `--objects-json` flag into typed objects. Raises if empty — a
	backup with no artifacts is a bug, not a silent no-op."""
	items = json.loads(objects_json)
	if not items:
		raise ValueError("objects-json is empty: nothing to move")
	return [BackupObject.from_plan(item) for item in items]
