from __future__ import annotations

from collections.abc import Callable

import boto3
from boto3.s3.transfer import S3UploadFailedError
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


class S3Error(Exception):
	"""Report an S3 operation failure."""


class S3Client:
	"""Access object storage through an S3-compatible API."""

	def __init__(
		self,
		*,
		bucket: str,
		access_key_id: str,
		secret_access_key: str,
		endpoint_url: str = "",
		region: str = "",
		signed_url_expiry: int = 86400,
	) -> None:
		if not bucket or not access_key_id or not secret_access_key:
			raise S3Error("S3 is not configured. Set the bucket and credentials in Atlas Settings.")
		self.bucket = bucket
		self.signed_url_expiry = signed_url_expiry
		self._client = boto3.client(
			"s3",
			endpoint_url=endpoint_url or None,
			region_name=region or None,
			aws_access_key_id=access_key_id,
			aws_secret_access_key=secret_access_key,
			config=Config(signature_version="s3v4"),
		)

	def upload_file(
		self, local_path: str, key: str, on_progress: Callable[[int], None] | None = None
	) -> None:
		"""Upload a file and report the byte count for each transferred part."""
		try:
			self._client.upload_file(local_path, self.bucket, key, Callback=on_progress)
		except (BotoCoreError, ClientError, S3UploadFailedError) as error:
			raise self._error("upload an object", key, error) from error

	def object_url(self, key: str, *, expiry_seconds: int | None = None) -> str:
		"""Return a signed object URL."""
		try:
			return self._client.generate_presigned_url(
				"get_object",
				Params={"Bucket": self.bucket, "Key": key},
				ExpiresIn=expiry_seconds or self.signed_url_expiry,
			)
		except (BotoCoreError, ClientError) as error:
			raise self._error("sign an object URL", key, error) from error

	def create_multipart_upload(self, key: str) -> str:
		"""Create a multipart upload and return its ID."""
		try:
			response = self._client.create_multipart_upload(Bucket=self.bucket, Key=key)
		except (BotoCoreError, ClientError) as error:
			raise self._error("create a multipart upload", key, error) from error
		upload_id = response.get("UploadId")
		if not isinstance(upload_id, str) or not upload_id:
			raise S3Error(f"S3 did not return a multipart upload ID for {key}")
		return upload_id

	def sign_upload_part(self, key: str, upload_id: str, part_number: int, *, expiry_seconds: int) -> str:
		"""Sign one multipart upload part."""
		try:
			return self._client.generate_presigned_url(
				"upload_part",
				Params={
					"Bucket": self.bucket,
					"Key": key,
					"UploadId": upload_id,
					"PartNumber": part_number,
				},
				ExpiresIn=expiry_seconds,
			)
		except (BotoCoreError, ClientError) as error:
			raise self._error("sign a multipart upload part", key, error) from error

	def list_multipart_parts(self, key: str, upload_id: str) -> list[dict[str, object]]:
		"""Return all uploaded parts in part number order."""
		parts = []
		part_number_marker = 0
		try:
			while True:
				response = self._client.list_parts(
					Bucket=self.bucket,
					Key=key,
					UploadId=upload_id,
					PartNumberMarker=part_number_marker,
				)
				parts.extend(response.get("Parts", []))
				if not response.get("IsTruncated"):
					return parts
				part_number_marker = response["NextPartNumberMarker"]
		except (BotoCoreError, ClientError) as error:
			raise self._error("list multipart upload parts", key, error) from error

	def complete_multipart_upload(self, key: str, upload_id: str, parts: list[dict[str, object]]) -> None:
		"""Complete a multipart upload with the supplied ETags."""
		completed_parts = [
			{
				"PartNumber": part.get("part_number", part.get("PartNumber")),
				"ETag": part.get("etag", part.get("ETag")),
			}
			for part in parts
		]
		try:
			self._client.complete_multipart_upload(
				Bucket=self.bucket,
				Key=key,
				UploadId=upload_id,
				MultipartUpload={"Parts": completed_parts},
			)
		except (BotoCoreError, ClientError) as error:
			raise self._error("complete a multipart upload", key, error) from error

	def abort_multipart_upload(self, key: str, upload_id: str) -> None:
		"""Abort an incomplete multipart upload."""
		try:
			self._client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
		except (BotoCoreError, ClientError) as error:
			raise self._error("abort a multipart upload", key, error) from error

	def head_object(self, key: str) -> dict[str, object] | None:
		"""Return object metadata, or None when the object is absent."""
		try:
			return self._client.head_object(Bucket=self.bucket, Key=key)
		except ClientError as error:
			if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
				return None
			raise self._error("read object metadata", key, error) from error
		except BotoCoreError as error:
			raise self._error("read object metadata", key, error) from error

	def delete_object(self, key: str) -> None:
		"""Delete one object."""
		try:
			self._client.delete_object(Bucket=self.bucket, Key=key)
		except (BotoCoreError, ClientError) as error:
			raise self._error("delete an object", key, error) from error

	@staticmethod
	def _error(operation: str, key: str, error: Exception) -> S3Error:
		code = "unknown"
		if isinstance(error, ClientError):
			code = str(error.response.get("Error", {}).get("Code") or code)
		return S3Error(f"Could not {operation} for {key}. S3 error code: {code}")
