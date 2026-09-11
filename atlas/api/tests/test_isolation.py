import frappe
from frappe.tests import IntegrationTestCase

from atlas.api.core.base import get_owned_document
from atlas.api.core.errors import ResourceNotFound
from atlas.api.routes.images import get_image
from atlas.api.routes.ip_addresses import get_ip_address, list_ip_addresses
from atlas.api.tests.test_images import insert_image
from atlas.api.tests.test_support import OTHER_TENANT_ID, TENANT_ID, api_request, call_route


def insert_ip_address(tenant_id: int) -> str:
	"""Insert one reserved IP address and return its name. The address names the record."""
	address = f"203.0.113.{frappe.db.count('Metal Server IP Address') + 20}"
	return (
		frappe.get_doc(
			{
				"doctype": "Metal Server IP Address",
				"address": address,
				"provider_resource_id": f"provider-{address}",
				"tenant_id": tenant_id,
				"status": "Allocated",
			}
		)
		.insert()
		.name
	)


class TestOwnedDocument(IntegrationTestCase):
	"""get_owned_document carries the tenant rule for every single-resource route."""

	def setUp(self) -> None:
		self.own_image = insert_image(TENANT_ID)
		self.other_image = insert_image(OTHER_TENANT_ID)

	def test_the_request_tenant_reads_its_own_document(self) -> None:
		with api_request(tenant_id=TENANT_ID):
			document = get_owned_document("Virtual Machine Image", self.own_image)

		self.assertEqual(document.name, self.own_image)

	def test_another_tenant_document_is_absent(self) -> None:
		with api_request(tenant_id=OTHER_TENANT_ID), self.assertRaises(ResourceNotFound):
			get_owned_document("Virtual Machine Image", self.own_image)

	def test_an_empty_name_is_absent(self) -> None:
		with api_request(tenant_id=TENANT_ID), self.assertRaises(ResourceNotFound) as failure:
			get_owned_document("Virtual Machine Image", "", "image")

		self.assertEqual(str(failure.exception), "The image does not exist.")

	def test_a_central_request_without_a_tenant_header_is_invalid(self) -> None:
		with api_request():
			status, body = call_route(get_image, image_id=self.own_image)

		self.assertEqual(status, 400)
		self.assertEqual(body["error"]["code"], "invalid_request")


class TestPermissionQueryConditions(IntegrationTestCase):
	"""A list query is tenant scoped by the Frappe permission hook, without a route filter."""

	def setUp(self) -> None:
		self.own_image = insert_image(TENANT_ID)
		self.other_image = insert_image(OTHER_TENANT_ID)
		self.system_image = insert_image(0, "system")

	def test_the_hook_filters_a_query_that_carries_no_tenant_filter(self) -> None:
		with api_request(tenant_id=TENANT_ID):
			names = {row.name for row in frappe.get_list("Virtual Machine Image", limit=100)}

		self.assertIn(self.own_image, names)
		self.assertIn(self.system_image, names)
		self.assertNotIn(self.other_image, names)

	def test_a_query_without_an_identity_or_a_role_is_refused(self) -> None:
		previous_user = frappe.session.user
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				frappe.get_list("Virtual Machine Image", limit=100)
		finally:
			frappe.set_user(previous_user)


class TestIPAddressIsolation(IntegrationTestCase):
	def setUp(self) -> None:
		self.own_address = insert_ip_address(TENANT_ID)
		self.other_address = insert_ip_address(OTHER_TENANT_ID)

	def test_a_list_holds_only_the_tenant_addresses(self) -> None:
		with api_request(
			"GET", "/api/atlas/ip-addresses", tenant_id=TENANT_ID, query_string={"limit": "100"}
		):
			status, body = call_route(list_ip_addresses)

		self.assertEqual(status, 200)
		names = {item["id"] for item in body["items"]}
		self.assertIn(self.own_address, names)
		self.assertNotIn(self.other_address, names)

	def test_another_tenant_cannot_read_the_address(self) -> None:
		with api_request("GET", "/api/atlas/ip-addresses/x", tenant_id=OTHER_TENANT_ID):
			status, body = call_route(get_ip_address, ip_address_id=self.own_address)

		self.assertEqual(status, 404)
		self.assertEqual(body["error"]["code"], "not_found")
