from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from atlas.api.models import ImageResponse
from atlas.api.routes.images import delete_image, download_image, get_image, list_images
from atlas.api.tests.test_support import OTHER_TENANT_ID, TENANT_ID, api_request, call_route
from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage

DOWNLOAD = {
	"artifact": "rootfs",
	"url": "https://storage/rootfs",
	"size_mib": 1024,
	"sha256": "a" * 64,
	"expires_in": 86400,
	"expires_at": "2026-09-09 10:00:00",
}


def insert_image(tenant_id: int, image_type: str = "machine", **overrides) -> str:
	"""Insert one Available virtual machine image and return its name."""
	values = {
		"doctype": "Virtual Machine Image",
		"title": f"test-image-{tenant_id}-{image_type}",
		"image_type": image_type,
		"tenant_id": tenant_id,
		"platform": "amd64",
		"operating_system": "Ubuntu",
		"operating_system_version": "24.04",
		"status": "Available",
		"enabled": 1,
		"image_object_key": "images/test/rootfs.img",
		"image_sha256": "a" * 64,
		"image_size_mib": 1024,
		"kernel_object_key": "images/test/kernel",
		"kernel_sha256": "b" * 64,
		"kernel_size_mib": 8,
	}
	values.update(overrides)
	return frappe.get_doc(values).insert().name


class TestImageView(UnitTestCase):
	def test_view_hides_the_object_keys(self) -> None:
		view = ImageResponse.from_document(
			SimpleNamespace(
				name="image-1",
				tenant_id=TENANT_ID,
				title="Ubuntu 24.04",
				image_type="machine",
				platform="amd64",
				operating_system="Ubuntu",
				operating_system_version="24.04",
				status="Available",
				enabled=1,
				supports_cloud_init=1,
				cache_image=0,
				memory_snapshot=0,
				image_size_mib=1024,
				kernel_size_mib=8,
				transfer_progress=100,
				transfer_error=None,
				creation="2026-09-08 10:00:00",
			)
		)

		self.assertEqual(view.status, "available")
		self.assertEqual(view.image_type, "machine")
		self.assertEqual(view.rootfs_size_mib, 1024)
		self.assertIsInstance(view.created_at, int)
		self.assertNotIn("image_object_key", view.model_fields)
		self.assertNotIn("kernel_object_key", view.model_fields)


class TestImageAccess(IntegrationTestCase):
	def setUp(self) -> None:
		self.system_image = insert_image(TENANT_ID, "system")
		self.zero_tenant_image = insert_image(0, "system")
		self.own_image = insert_image(TENANT_ID)
		self.other_image = insert_image(OTHER_TENANT_ID)

	def list_names(self, tenant_id: int) -> set[str]:
		"""Return the image identifiers that one tenant can list."""
		with api_request("GET", "/api/atlas/images", tenant_id=tenant_id, query_string={"limit": "100"}):
			status, body = call_route(list_images)

		self.assertEqual(status, 200)
		return {item["id"] for item in body["items"]}

	def test_list_includes_shared_system_images(self) -> None:
		names = self.list_names(TENANT_ID)

		self.assertIn(self.system_image, names)
		self.assertIn(self.own_image, names)
		self.assertNotIn(self.other_image, names)
		self.assertIn(self.zero_tenant_image, names)

	def test_another_tenant_cannot_read_a_machine_image(self) -> None:
		with api_request("GET", "/api/atlas/images/x", tenant_id=OTHER_TENANT_ID):
			status, body = call_route(get_image, image_id=self.own_image)

		self.assertEqual(status, 404)
		self.assertEqual(body["error"]["code"], "not_found")

	def test_a_tenant_reads_a_shared_system_image(self) -> None:
		with api_request("GET", "/api/atlas/images/x", tenant_id=TENANT_ID):
			status, body = call_route(get_image, image_id=self.zero_tenant_image)

		self.assertEqual(status, 200)
		self.assertEqual(body["tenant_id"], 0)


class TestImageDownload(IntegrationTestCase):
	def test_download_is_never_cached(self) -> None:
		image_name = insert_image(TENANT_ID)
		with (
			api_request(
				"GET",
				"/api/atlas/images/x/download",
				tenant_id=TENANT_ID,
				query_string={"artifact": "rootfs"},
			),
			patch.object(
				VirtualMachineImage, "get_presigned_download_url", return_value=DOWNLOAD
			) as get_download,
		):
			response = download_image(image_id=image_name)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.headers["Cache-Control"], "no-store")
		self.assertEqual(response.get_json()["artifact"], "rootfs")
		self.assertIsInstance(response.get_json()["expires_at"], int)
		get_download.assert_called_once_with("rootfs")

	def test_download_needs_a_known_artifact(self) -> None:
		for query_string in ({}, {"artifact": "disk"}):
			with api_request(
				"GET",
				"/api/atlas/images/x/download",
				tenant_id=TENANT_ID,
				query_string=query_string,
			):
				status, body = call_route(download_image, image_id="image-1")

			self.assertEqual(status, 400)
			self.assertEqual(body["error"]["code"], "invalid_request")

	def test_another_tenant_cannot_download_the_image(self) -> None:
		image_name = insert_image(TENANT_ID)
		with api_request(
			"GET",
			"/api/atlas/images/x/download",
			tenant_id=OTHER_TENANT_ID,
			query_string={"artifact": "kernel"},
		):
			status, body = call_route(download_image, image_id=image_name)

		self.assertEqual(status, 404)
		self.assertEqual(body["error"]["code"], "not_found")


class TestImageDeletion(IntegrationTestCase):
	def delete(self, image_name: str, tenant_id: int):
		"""Run the delete route against one stored image."""
		with (
			api_request("DELETE", "/api/atlas/images/x", tenant_id=tenant_id),
			patch.object(VirtualMachineImage, "request_deletion") as request_deletion,
		):
			return (*call_route(delete_image, image_id=image_name), request_deletion)

	def test_deletion_starts_and_reports_the_record(self) -> None:
		image_name = insert_image(TENANT_ID)
		status, body, request_deletion = self.delete(image_name, TENANT_ID)

		self.assertEqual(status, 202)
		self.assertEqual(body["id"], image_name)
		request_deletion.assert_called_once()

	def test_the_owner_deletes_its_own_system_image(self) -> None:
		image_name = insert_image(TENANT_ID, "system")
		status, body, request_deletion = self.delete(image_name, TENANT_ID)

		self.assertEqual(status, 202)
		self.assertEqual(body["id"], image_name)
		request_deletion.assert_called_once()

	def test_a_system_image_of_another_tenant_cannot_be_deleted(self) -> None:
		image_name = insert_image(0, "system")
		status, body, request_deletion = self.delete(image_name, TENANT_ID)

		self.assertEqual(status, 409)
		self.assertEqual(body["error"]["code"], "conflict")
		request_deletion.assert_not_called()

	def test_another_tenant_cannot_delete_the_image(self) -> None:
		image_name = insert_image(TENANT_ID)
		status, body, request_deletion = self.delete(image_name, OTHER_TENANT_ID)

		self.assertEqual(status, 404)
		self.assertEqual(body["error"]["code"], "not_found")
		request_deletion.assert_not_called()
