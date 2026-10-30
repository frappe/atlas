from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from botocore.exceptions import ClientError
from frappe.tests import UnitTestCase

from atlas.atlas.object_storage import ObjectStorageClient, ObjectStorageError


class TestObjectStorageClient(UnitTestCase):
	def test_signed_get_and_put_urls_share_the_object_but_not_the_signature(self) -> None:
		client = ObjectStorageClient(
			bucket="snapshots",
			access_key_id="test-access",
			secret_access_key="test-secret",
			endpoint_url="https://objects.example",
			region="test-region",
		)
		key = "sleep/site/host/vm-1/memory"
		download = urlsplit(client.object_url(key))
		upload = urlsplit(client.object_url(key, method="PUT"))

		self.assertEqual(
			(download.scheme, download.netloc, download.path), (upload.scheme, upload.netloc, upload.path)
		)
		self.assertNotEqual(
			parse_qs(download.query)["X-Amz-Signature"], parse_qs(upload.query)["X-Amz-Signature"]
		)

	def make_client(self) -> tuple[ObjectStorageClient, Mock]:
		boto_client = Mock()
		with patch("atlas.atlas.object_storage.boto3.client", return_value=boto_client):
			client = ObjectStorageClient(bucket="bucket", access_key_id="access", secret_access_key="secret")
		return client, boto_client

	def test_create_and_sign_multipart_upload(self) -> None:
		client, boto_client = self.make_client()
		boto_client.create_multipart_upload.return_value = {"UploadId": "upload-1"}
		boto_client.generate_presigned_url.return_value = "https://upload.test"

		upload_id = client.create_multipart_upload("images/image/rootfs.img")
		url = client.sign_upload_part("images/image/rootfs.img", upload_id, 2, expiry_seconds=86400)

		self.assertEqual(upload_id, "upload-1")
		self.assertEqual(url, "https://upload.test")
		self.assertEqual(boto_client.generate_presigned_url.call_args.kwargs["ExpiresIn"], 86400)
		self.assertEqual(boto_client.generate_presigned_url.call_args.kwargs["Params"]["PartNumber"], 2)

	def test_complete_normalizes_metal_parts(self) -> None:
		client, boto_client = self.make_client()

		client.complete_multipart_upload(
			"images/image/kernel",
			"upload-1",
			[{"part_number": 1, "etag": '"etag-1"'}],
		)

		self.assertEqual(
			boto_client.complete_multipart_upload.call_args.kwargs["MultipartUpload"],
			{"Parts": [{"PartNumber": 1, "ETag": '"etag-1"'}]},
		)

	def test_list_parts_follows_pagination(self) -> None:
		client, boto_client = self.make_client()
		boto_client.list_parts.side_effect = [
			{
				"Parts": [{"PartNumber": 1, "ETag": "one"}],
				"IsTruncated": True,
				"NextPartNumberMarker": 1,
			},
			{"Parts": [{"PartNumber": 2, "ETag": "two"}], "IsTruncated": False},
		]

		parts = client.list_multipart_parts("images/image/rootfs.img", "upload-1")

		self.assertEqual([part["PartNumber"] for part in parts], [1, 2])

	def test_abort_accepts_an_upload_that_is_already_gone(self) -> None:
		"""A completed or already aborted upload is the state the caller wants."""
		client, boto_client = self.make_client()
		boto_client.abort_multipart_upload.side_effect = ClientError(
			{"Error": {"Code": "NoSuchUpload"}}, "AbortMultipartUpload"
		)

		client.abort_multipart_upload("images/image/rootfs.img", "upload-1")

	def test_abort_reports_a_real_failure(self) -> None:
		client, boto_client = self.make_client()
		boto_client.abort_multipart_upload.side_effect = ClientError(
			{"Error": {"Code": "AccessDenied"}}, "AbortMultipartUpload"
		)

		with self.assertRaises(ObjectStorageError):
			client.abort_multipart_upload("images/image/rootfs.img", "upload-1")
