"""S3 Settings — the S3 bucket + credentials for snapshot backups.

The secret is read via `atlas.atlas.secrets.get_secret` by
`atlas.atlas.s3.S3Backup`; the host never sees it (it gets only presigned URLs).
`test_connection` is the Test Connection button (head_bucket). See
spec/29-snapshot-backup.md.
"""

from __future__ import annotations

import dataclasses

import frappe
from frappe.model.document import Document


class S3Settings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		access_key_id: DF.Data
		bucket: DF.Data
		endpoint_url: DF.Data | None
		key_prefix: DF.Data | None
		presign_expiry_seconds: DF.Int
		region: DF.Data | None
		secret_access_key: DF.Password
	# end: auto-generated types

	@frappe.whitelist()
	def setup(
		self,
		bucket: str,
		access_key_id: str,
		secret_access_key: str,
		region: str = "us-east-1",
		endpoint_url: str = "",
		key_prefix: str = "atlas/snapshots",
		presign_expiry_seconds: int = 3600,
	) -> None:
		"""Explicit, idempotent setter for S3 Settings (the contract).

		Writes through `doc.save()` so the `secret_access_key` Password goes through
		the normal ORM path (`_save_passwords` encrypts to `__Auth` AND stamps the
		field placeholder, so the desk form shows it as set). Idempotent — re-running
		just overwrites the Single."""
		self.bucket = bucket
		self.access_key_id = access_key_id
		self.secret_access_key = secret_access_key
		self.region = region or "us-east-1"
		self.endpoint_url = endpoint_url or ""
		self.key_prefix = key_prefix or "atlas/snapshots"
		self.presign_expiry_seconds = int(presign_expiry_seconds or 3600)
		self.save(ignore_permissions=True)

	@frappe.whitelist()
	def test_connection(self) -> dict:
		"""Test Connection button — prove the credentials reach the bucket."""
		from atlas.atlas.s3 import S3Backup

		return dataclasses.asdict(S3Backup().test_connection())
