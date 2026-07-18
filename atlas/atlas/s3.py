"""S3 snapshot backup — the controller-side object-storage client.

Reads `S3 Settings` for the bucket + credentials (the secret via
`atlas.atlas.secrets.get_secret`, mirroring `DigitalOceanProvider` / `Route53`).
The host never holds S3 credentials: this class **presigns** short-lived PUT/GET
URLs that the host streams to/from with plain `curl`
(spec/29-snapshot-backup.md). `boto3` is a controller-only dependency (already
required for TLS); the import is local so it never loads on a host or at module
import.
"""

from __future__ import annotations

import dataclasses

import frappe

from atlas.atlas.secrets import get_secret

DEFAULT_REGION = "us-east-1"
DEFAULT_KEY_PREFIX = "atlas/snapshots"
DEFAULT_PRESIGN_EXPIRY_SECONDS = 3600


@dataclasses.dataclass(frozen=True)
class TestResult:
	"""Outcome of the S3 Settings Test Connection button."""

	ok: bool
	detail: str = ""


def is_configured() -> bool:
	"""True iff `S3 Settings` has a bucket and an access key — the cheap gate a
	controller checks before instantiating `S3Backup` (which throws otherwise)."""
	settings = frappe.get_single("S3 Settings")
	return bool(settings.bucket and settings.access_key_id)


def backup_plan(snapshot) -> list[dict]:
	"""The artifacts of `snapshot` to push to (or pull from) S3 — one dict per
	object. This is the single place that knows *which* on-host files back a
	snapshot: the root disk LV, the data disk LV (cold-with-data), and the warm
	memory pair + host signature. Pure over the doc's fields, so it unit-tests
	with a stub. Each dict is the object's identity; the transport (`put_url` on
	upload, `sha256`/`get_url` on restore) is layered on by the caller.

	`disk_gigabytes` is the LV size to recreate on restore (0 for a file object).
	`compress` is zstd for everything but the tiny host-signature JSON.
	"""
	plan: list[dict] = [_disk_object("rootfs", snapshot.rootfs_path, snapshot.disk_gigabytes)]
	if snapshot.data_rootfs_path:
		plan.append(_disk_object("data", snapshot.data_rootfs_path, snapshot.data_disk_gigabytes))
	if snapshot.kind == "Warm" and snapshot.memory_directory:
		directory = snapshot.memory_directory.rstrip("/")
		plan.append(_file_object("vmstate", f"{directory}/vmstate.bin", "vmstate.bin.zst", compress=True))
		plan.append(_file_object("mem", f"{directory}/mem.bin", "mem.bin.zst", compress=True))
		plan.append(
			_file_object(
				"host-signature", f"{directory}/host-signature.json", "host-signature.json", compress=False
			)
		)
	return plan


def _disk_object(name: str, source: str, disk_gigabytes: int) -> dict:
	return {
		"name": name,
		"object_name": f"{name}.img.zst",
		"source": source,
		"block": True,
		"compress": True,
		"disk_gigabytes": int(disk_gigabytes or 0),
	}


def _file_object(name: str, source: str, object_name: str, *, compress: bool) -> dict:
	return {
		"name": name,
		"object_name": object_name,
		"source": source,
		"block": False,
		"compress": compress,
		"disk_gigabytes": 0,
	}


class S3Backup:
	"""The S3 bucket that holds snapshot backups. Presigns per-object URLs, owns
	the key layout, and can delete a snapshot's objects; the byte movement itself
	happens on the host over `curl` (this class never touches a disk)."""

	def __init__(self) -> None:
		settings = frappe.get_single("S3 Settings")
		if not settings.bucket or not settings.access_key_id:
			frappe.throw("S3 Settings is not configured — set the bucket and credentials first.")
		self.bucket = settings.bucket
		self.region = settings.region or DEFAULT_REGION
		self.endpoint_url = settings.endpoint_url or None
		self.key_prefix = (settings.key_prefix or DEFAULT_KEY_PREFIX).strip("/")
		self.expiry_seconds = int(settings.presign_expiry_seconds or DEFAULT_PRESIGN_EXPIRY_SECONDS)
		self.access_key_id = settings.access_key_id
		self.secret_access_key = get_secret("S3 Settings", "S3 Settings", "secret_access_key")

	def object_key(self, snapshot_name: str, object_name: str) -> str:
		"""`<prefix>/<snapshot>/<object>` — the single source of the key layout."""
		return f"{self.key_prefix}/{snapshot_name}/{object_name}"

	def prefix_for(self, snapshot_name: str) -> str:
		"""The key prefix that holds every object of one snapshot."""
		return f"{self.key_prefix}/{snapshot_name}/"

	def presign_put(self, key: str) -> str:
		return self._presign("put_object", key)

	def presign_get(self, key: str) -> str:
		return self._presign("get_object", key)

	def delete_prefix(self, snapshot_name: str) -> int:
		"""Delete every object under a snapshot's prefix; return how many. Used by
		the snapshot row's on_trash so a deleted backup leaves no paid orphan."""
		client = self._client()
		prefix = self.prefix_for(snapshot_name)
		deleted = 0
		paginator = client.get_paginator("list_objects_v2")
		for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
			keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
			if keys:
				client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})
				deleted += len(keys)
		return deleted

	def test_connection(self) -> TestResult:
		"""head_bucket — the lightest read that proves the credentials reach the
		bucket, mirroring Route53Settings.test_connection's list_hosted_zones."""
		try:
			self._client().head_bucket(Bucket=self.bucket)
		except ImportError:
			return TestResult(ok=False, detail="boto3 not installed on the controller")
		except Exception as exception:
			return TestResult(ok=False, detail=str(exception))
		return TestResult(ok=True, detail=f"reached bucket {self.bucket!r} in {self.region}")

	# --- boto3 (controller-only; imported locally so hosts never load it) ---

	def _client(self):
		import boto3
		from botocore.client import Config

		# s3v4 presigning works in every region; path-style addressing is what
		# S3-compatible stores (MinIO, DO Spaces on a custom endpoint) need.
		style = "path" if self.endpoint_url else "auto"
		return boto3.client(
			"s3",
			aws_access_key_id=self.access_key_id,
			aws_secret_access_key=self.secret_access_key,
			region_name=self.region,
			endpoint_url=self.endpoint_url,
			config=Config(signature_version="s3v4", s3={"addressing_style": style}),
		)

	def _presign(self, method: str, key: str) -> str:
		return self._client().generate_presigned_url(
			method, Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=self.expiry_seconds
		)
