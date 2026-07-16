"""Unit tests for atlas.atlas.s3 — the snapshot-backup S3 client.

`backup_plan` is pure over the snapshot's fields (tested with stubs, no DB). The
key-layout / presign methods need `S3 Settings` configured; presign itself needs
boto3 (a controller-only dep), so those assertions skip cleanly when it is absent.
See spec/29-snapshot-backup.md.
"""

import importlib.util
import types
import unittest

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import s3


def _boto3_available() -> bool:
	return importlib.util.find_spec("boto3") is not None


def _snapshot(**overrides) -> types.SimpleNamespace:
	base = {
		"rootfs_path": "/dev/atlas/atlas-snap-abc",
		"data_rootfs_path": "",
		"data_disk_gigabytes": 0,
		"disk_gigabytes": 28,
		"kind": "Cold",
		"memory_directory": "",
	}
	base.update(overrides)
	return types.SimpleNamespace(**base)


class TestBackupPlan(unittest.TestCase):
	def test_cold_root_only(self) -> None:
		plan = s3.backup_plan(_snapshot())
		self.assertEqual([obj["name"] for obj in plan], ["rootfs"])
		root = plan[0]
		self.assertEqual(root["object_name"], "rootfs.img.zst")
		self.assertEqual(root["source"], "/dev/atlas/atlas-snap-abc")
		self.assertTrue(root["block"])
		self.assertTrue(root["compress"])
		self.assertEqual(root["disk_gigabytes"], 28)

	def test_cold_with_data(self) -> None:
		plan = s3.backup_plan(
			_snapshot(data_rootfs_path="/dev/atlas/atlas-datasnap-abc", data_disk_gigabytes=10)
		)
		self.assertEqual([obj["name"] for obj in plan], ["rootfs", "data"])
		self.assertEqual(plan[1]["object_name"], "data.img.zst")
		self.assertEqual(plan[1]["source"], "/dev/atlas/atlas-datasnap-abc")
		self.assertEqual(plan[1]["disk_gigabytes"], 10)

	def test_warm_adds_memory_pair(self) -> None:
		plan = s3.backup_plan(_snapshot(kind="Warm", memory_directory="/var/lib/atlas/snapshots/abc"))
		by_name = {obj["name"]: obj for obj in plan}
		self.assertEqual([obj["name"] for obj in plan], ["rootfs", "vmstate", "mem", "host-signature"])
		self.assertEqual(by_name["vmstate"]["source"], "/var/lib/atlas/snapshots/abc/vmstate.bin")
		self.assertEqual(by_name["vmstate"]["object_name"], "vmstate.bin.zst")
		self.assertFalse(by_name["mem"]["block"])
		self.assertTrue(by_name["mem"]["compress"])
		# The host signature ships raw (no zstd) — it is tiny.
		self.assertFalse(by_name["host-signature"]["compress"])
		self.assertEqual(by_name["host-signature"]["object_name"], "host-signature.json")

	def test_warm_trailing_slash_is_trimmed(self) -> None:
		plan = s3.backup_plan(_snapshot(kind="Warm", memory_directory="/m/abc/"))
		vmstate = next(obj for obj in plan if obj["name"] == "vmstate")
		self.assertEqual(vmstate["source"], "/m/abc/vmstate.bin")


class TestS3Backup(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.get_doc("S3 Settings").setup(
			bucket="atlas-backups-test",
			access_key_id="AKIATEST",
			secret_access_key="secret-xyz",
			region="us-west-2",
			key_prefix="atlas/snapshots",
		)

	def test_key_layout(self) -> None:
		backup = s3.S3Backup()
		self.assertEqual(backup.object_key("snap1", "rootfs.img.zst"), "atlas/snapshots/snap1/rootfs.img.zst")
		self.assertEqual(backup.prefix_for("snap1"), "atlas/snapshots/snap1/")
		self.assertEqual(backup.bucket, "atlas-backups-test")

	def test_key_prefix_is_normalized(self) -> None:
		frappe.get_doc("S3 Settings").setup(
			bucket="b", access_key_id="A", secret_access_key="S", key_prefix="/atlas/snaps/"
		)
		self.assertEqual(s3.S3Backup().prefix_for("x"), "atlas/snaps/x/")

	def test_is_configured(self) -> None:
		self.assertTrue(s3.is_configured())

	def test_unconfigured_throws(self) -> None:
		frappe.db.set_single_value("S3 Settings", "bucket", "")
		self.assertFalse(s3.is_configured())
		with self.assertRaises(frappe.ValidationError):
			s3.S3Backup()

	@unittest.skipUnless(_boto3_available(), "boto3 not installed on the controller")
	def test_presign_embeds_bucket_and_key(self) -> None:
		backup = s3.S3Backup()
		url = backup.presign_put(backup.object_key("snap1", "rootfs.img.zst"))
		self.assertTrue(url.startswith("https://"))
		self.assertIn("atlas-backups-test", url)
		self.assertIn("snap1", url)
