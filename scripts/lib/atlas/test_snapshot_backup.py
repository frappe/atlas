"""Unit tests for the pure snapshot-backup plan parsing shared by
upload-snapshot-s3.py / restore-snapshot-s3.py.

Run with bare `python3 -m unittest atlas.test_snapshot_backup` from scripts/lib:
no Frappe, no site, no host. Covers the JSON contract the controller and the two
host scripts agree on (spec/29-snapshot-backup.md).
"""

import json
import unittest

from atlas.snapshot_backup import BackupObject, parse_objects


class TestBackupObject(unittest.TestCase):
	def test_from_plan_full(self) -> None:
		obj = BackupObject.from_plan(
			{
				"name": "rootfs",
				"object_name": "rootfs.img.zst",
				"source": "/dev/atlas/atlas-snap-x",
				"block": True,
				"compress": True,
				"disk_gigabytes": 28,
				"url": "https://s3.example/put",
				"sha256": "abc123",
			}
		)
		self.assertEqual(obj.name, "rootfs")
		self.assertEqual(obj.object_name, "rootfs.img.zst")
		self.assertEqual(obj.source, "/dev/atlas/atlas-snap-x")
		self.assertTrue(obj.block)
		self.assertTrue(obj.compress)
		self.assertEqual(obj.disk_gigabytes, 28)
		self.assertEqual(obj.url, "https://s3.example/put")
		self.assertEqual(obj.sha256, "abc123")

	def test_from_plan_defaults(self) -> None:
		# The upload plan carries no sha256/url yet, and a file object no disk size.
		obj = BackupObject.from_plan(
			{
				"name": "host-signature",
				"object_name": "host-signature.json",
				"source": "/var/lib/atlas/snapshots/x/host-signature.json",
				"block": False,
				"compress": False,
			}
		)
		self.assertEqual(obj.disk_gigabytes, 0)
		self.assertEqual(obj.url, "")
		self.assertEqual(obj.sha256, "")
		self.assertFalse(obj.block)
		self.assertFalse(obj.compress)

	def test_from_plan_coerces_types(self) -> None:
		# JSON round-trips can deliver block as 1 and disk_gigabytes as a string.
		obj = BackupObject.from_plan(
			{
				"name": "data",
				"object_name": "data.img.zst",
				"source": "/dev/atlas/atlas-datasnap-x",
				"block": 1,
				"compress": 1,
				"disk_gigabytes": "10",
			}
		)
		self.assertIs(obj.block, True)
		self.assertIs(obj.compress, True)
		self.assertEqual(obj.disk_gigabytes, 10)

	def test_is_frozen(self) -> None:
		obj = BackupObject.from_plan(
			{"name": "r", "object_name": "r.zst", "source": "/x", "block": True, "compress": True}
		)
		with self.assertRaises(Exception):
			obj.name = "y"  # type: ignore[misc]


class TestParseObjects(unittest.TestCase):
	def test_parse_list(self) -> None:
		objects = parse_objects(
			json.dumps(
				[
					{
						"name": "rootfs",
						"object_name": "rootfs.img.zst",
						"source": "/dev/x",
						"block": True,
						"compress": True,
						"disk_gigabytes": 4,
					},
					{
						"name": "mem",
						"object_name": "mem.bin.zst",
						"source": "/m/mem.bin",
						"block": False,
						"compress": True,
					},
				]
			)
		)
		self.assertEqual([obj.name for obj in objects], ["rootfs", "mem"])
		self.assertTrue(objects[0].block)
		self.assertFalse(objects[1].block)

	def test_empty_raises(self) -> None:
		with self.assertRaises(ValueError):
			parse_objects("[]")


if __name__ == "__main__":
	unittest.main()
