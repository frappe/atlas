from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from atlas.api.core.errors import InvalidRequest
from atlas.api.tests.test_support import api_request
from atlas.auth.identity import (
	CENTRAL_TENANT,
	MAXIMUM_TENANT_ID,
	SESSION_ISSUER,
	TENANT_HEADER,
	AtlasIdentity,
	current_identity,
	get_current_tenant_id,
	parse_tenant_id,
	set_identity,
)
from atlas.auth.overrides import get_permission_query_conditions, has_permission
from atlas.auth.request import validate_auth
from atlas.auth.roles import has_role
from atlas.vm.doctype.virtual_machine_image.virtual_machine_image import VirtualMachineImage


def build_image(tenant_id: int, image_type: str) -> VirtualMachineImage:
	"""Return one image document that answers the tenant visibility rule."""
	image = VirtualMachineImage.__new__(VirtualMachineImage)
	image.doctype = "Virtual Machine Image"
	image.tenant_id = tenant_id
	image.image_type = image_type
	return image


def claims(tenant: str, subject: str = "cargo", scope: str = "*") -> dict:
	"""Return the validated claims of one Atlas API token."""
	return {"iss": "central", "sub": subject, "tenant": tenant, "scope": scope}


class TestTenantValue(UnitTestCase):
	def test_accepted_tenant_values(self) -> None:
		self.assertEqual(parse_tenant_id("0"), 0)
		self.assertEqual(parse_tenant_id("1"), 1)
		self.assertEqual(parse_tenant_id(" 42 "), 42)
		self.assertEqual(parse_tenant_id(str(MAXIMUM_TENANT_ID)), MAXIMUM_TENANT_ID)

	def test_rejected_tenant_values(self) -> None:
		for value in ("", "   ", "abc", "1.5", "-1", str(MAXIMUM_TENANT_ID + 1)):
			with self.assertRaises(InvalidRequest):
				parse_tenant_id(value)

	def test_failure_names_the_header(self) -> None:
		with self.assertRaises(InvalidRequest) as failure:
			parse_tenant_id("abc")

		self.assertEqual(failure.exception.fields[0]["name"], TENANT_HEADER)
		self.assertEqual(failure.exception.http_status_code, 400)


class TestAtlasIdentity(UnitTestCase):
	def test_a_regional_claim_becomes_a_tenant_identity(self) -> None:
		identity = AtlasIdentity.from_claims(claims("7"))

		self.assertEqual(identity.tenant, "7")
		self.assertFalse(identity.is_central)

	def test_a_central_claim_serves_every_tenant(self) -> None:
		identity = AtlasIdentity.from_claims(claims(CENTRAL_TENANT, subject="central"))

		self.assertTrue(identity.is_central)

	def test_an_unusable_claim_has_no_identity(self) -> None:
		for changes in ({"tenant": ""}, {"tenant": "abc"}, {"subject": ""}):
			values = claims(changes.get("tenant", "7"), subject=changes.get("subject", "cargo"))
			self.assertIsNone(AtlasIdentity.from_claims(values))

	def test_a_missing_claim_has_no_identity(self) -> None:
		self.assertIsNone(AtlasIdentity.from_claims({"iss": "central", "sub": "cargo", "scope": "*"}))


class TestRequestTenant(UnitTestCase):
	def test_a_regional_identity_accepts_its_own_header(self) -> None:
		with api_request(tenant_id=7, send_tenant_header=True):
			self.assertEqual(get_current_tenant_id(), 7)

	def test_a_regional_identity_refuses_another_tenant_header(self) -> None:
		with api_request(tenant_id=7, headers={TENANT_HEADER: "9"}), self.assertRaises(InvalidRequest):
			get_current_tenant_id()

	def test_a_regional_identity_needs_no_header(self) -> None:
		with api_request(tenant_id=7):
			self.assertEqual(get_current_tenant_id(), 7)

	def test_a_central_identity_reads_the_header(self) -> None:
		with api_request(headers={TENANT_HEADER: "9"}):
			self.assertEqual(get_current_tenant_id(), 9)

	def test_a_central_identity_without_a_header_is_refused(self) -> None:
		with api_request(), self.assertRaises(InvalidRequest):
			get_current_tenant_id()

	def test_no_identity_has_no_tenant(self) -> None:
		previous_request = getattr(frappe.local, "request", None)
		previous_identity = getattr(frappe.local, "atlas_identity", None)
		frappe.local.request = None
		frappe.local.atlas_identity = None
		try:
			with self.assertRaises(frappe.PermissionError):
				get_current_tenant_id()
		finally:
			frappe.local.request = previous_request
			frappe.local.atlas_identity = previous_identity


class TestTenantDocumentPermissions(UnitTestCase):
	def test_has_role_reads_the_cached_roles(self) -> None:
		with patch("frappe.get_roles", return_value=["Atlas Admin"]) as get_roles:
			self.assertTrue(has_role("Atlas Admin", "atlas@example.com"))

		get_roles.assert_called_once_with("atlas@example.com")

	def test_a_list_query_is_filtered_by_the_identity_tenant(self) -> None:
		with api_request(tenant_id=9):
			condition = get_permission_query_conditions(doctype="Virtual Machine")

		self.assertEqual(condition, "`tabVirtual Machine`.`tenant_id` = 9")

	def test_a_central_identity_without_a_tenant_lists_nothing(self) -> None:
		with api_request():
			condition = get_permission_query_conditions(doctype="Virtual Machine")

		self.assertEqual(condition, "1=0")

	def test_another_tenant_document_is_refused(self) -> None:
		document = frappe._dict(doctype="Virtual Machine", tenant_id=8)
		with api_request(tenant_id=7):
			self.assertFalse(has_permission(document, "read"))

	def test_a_system_image_is_readable_but_not_writable(self) -> None:
		document = build_image(tenant_id=0, image_type="system")
		with api_request(tenant_id=7):
			self.assertTrue(has_permission(document, "read"))
			self.assertFalse(has_permission(document, "write"))
			self.assertFalse(has_permission(document, "delete"))

	def test_system_images_are_in_the_image_permission_query(self) -> None:
		with api_request(tenant_id=7):
			condition = get_permission_query_conditions(doctype="Virtual Machine Image")

		self.assertIn("tenant_id` = 7", condition)
		self.assertIn("image_type` = 'system'", condition)

	def test_an_identity_filters_a_system_manager_too(self) -> None:
		with api_request(tenant_id=7), patch("atlas.auth.overrides.has_role", return_value=True):
			condition = get_permission_query_conditions(doctype="Virtual Machine")

		self.assertEqual(condition, "`tabVirtual Machine`.`tenant_id` = 7")

	def test_a_system_manager_outside_a_request_keeps_full_access(self) -> None:
		with patch("atlas.auth.overrides.has_role", return_value=True):
			self.assertEqual(get_permission_query_conditions(doctype="Virtual Machine"), "")
			self.assertTrue(has_permission(frappe._dict(doctype="Virtual Machine", tenant_id=8), "read"))

	def test_a_caller_without_an_identity_or_role_sees_nothing(self) -> None:
		with patch("atlas.auth.overrides.has_role", return_value=False):
			self.assertEqual(get_permission_query_conditions(doctype="Virtual Machine"), "1=0")
			self.assertFalse(has_permission(frappe._dict(doctype="Virtual Machine", tenant_id=8), "read"))

	def test_an_unhandled_doctype_is_denied(self) -> None:
		document = frappe._dict(doctype="Unmanaged Atlas Document", tenant_id=7)
		with api_request(tenant_id=7):
			self.assertEqual(get_permission_query_conditions(doctype=document.doctype), "1=0")
			self.assertFalse(has_permission(document, "read"))


class TestGuestPaths(UnitTestCase):
	def setUp(self) -> None:
		self.previous_user = frappe.session.user
		frappe.set_user("Guest")

	def tearDown(self) -> None:
		frappe.set_user(self.previous_user)

	def test_a_guest_may_sign_in_and_read_the_reference(self) -> None:
		for path in (
			"/login",
			"/api/method/login",
			"/api/method/logout",
			"/assets/atlas/app.js",
			"/api/atlas/docs",
			"/api/atlas/docs/openapi.json",
			"/api/atlas/jwks.json",
		):
			with api_request(path=path):
				validate_auth()

	def test_a_guest_may_open_the_realtime_connection(self) -> None:
		for path in ("/socket.io/", "/api/method/frappe.realtime.get_user_info"):
			with api_request(path=path):
				validate_auth()

	def test_a_signed_in_user_may_read_the_public_jwks(self) -> None:
		frappe.set_user("ordinary@example.com")
		with api_request(path="/api/atlas/jwks.json"):
			validate_auth()

	def test_a_guest_cannot_use_another_route(self) -> None:
		for path in (
			"/",
			"/api/atlas/images",
			"/api/atlas/docs-old",
			"/api/resource/User",
			"/api/method/frappe.client.get_list",
		):
			with api_request(path=path), self.assertRaises(frappe.PermissionError):
				validate_auth()


class TestAuthValidator(UnitTestCase):
	def test_an_atlas_route_needs_an_identity(self) -> None:
		with (
			api_request(path="/api/atlas/images"),
			patch("atlas.auth.request.has_role", return_value=False),
			patch("atlas.auth.request.authenticate_token"),
			self.assertRaises(frappe.PermissionError),
		):
			validate_auth()

	def test_a_non_atlas_user_is_left_to_frappe(self) -> None:
		with api_request(path="/api/resource/User"), patch("atlas.auth.request.has_role", return_value=False):
			validate_auth()

	def test_an_identity_can_use_only_atlas_routes(self) -> None:
		def has_atlas_role(role: str, user: str | None = None) -> bool:
			return role == "Atlas Admin"

		def sign_in() -> None:
			set_identity(AtlasIdentity(subject="cargo", issuer="atlas:1", tenant="7", scope="*"))

		with (
			api_request(path="/api/atlas/images"),
			patch("atlas.auth.request.has_role", side_effect=has_atlas_role),
			patch("atlas.auth.request.authenticate_token", side_effect=sign_in),
		):
			validate_auth()
			self.assertEqual(current_identity().tenant, "7")

		with (
			api_request(path="/api/resource/User"),
			patch("atlas.auth.request.has_role", side_effect=has_atlas_role),
			patch("atlas.auth.request.authenticate_token", side_effect=sign_in),
			self.assertRaises(frappe.PermissionError),
		):
			validate_auth()

	def test_a_system_manager_acts_for_every_tenant_on_an_atlas_route(self) -> None:
		with (
			api_request(path="/api/atlas/images"),
			patch(
				"atlas.auth.request.has_role", side_effect=lambda role, user=None: role == "System Manager"
			),
			patch("atlas.auth.request.authenticate_token"),
		):
			validate_auth()
			identity = current_identity()

		self.assertEqual(identity.issuer, SESSION_ISSUER)
		self.assertTrue(identity.is_central)

	def test_realtime_is_open_to_an_atlas_admin(self) -> None:
		"""The console opens one Socket.IO connection, which asks the web process who the user is."""

		def has_atlas_role(role: str, user: str | None = None) -> bool:
			return role == "Atlas Admin"

		for path in (
			"/socket.io/",
			"/api/method/frappe.realtime.get_user_info",
			"/api/method/frappe.realtime.has_permission",
		):
			with (
				api_request(path=path),
				patch("atlas.auth.request.has_role", side_effect=has_atlas_role),
			):
				validate_auth()

	def test_realtime_is_open_to_a_user_without_atlas_role(self) -> None:
		previous_user = frappe.session.user
		frappe.set_user("user@example.com")
		try:
			with (
				api_request(path="/socket.io/"),
				patch("atlas.auth.request.has_role", return_value=False),
			):
				validate_auth()
		finally:
			frappe.set_user(previous_user)

	def test_a_system_manager_can_use_any_route(self) -> None:
		with (
			api_request(path="/api/resource/User"),
			patch(
				"atlas.auth.request.has_role", side_effect=lambda role, user=None: role == "System Manager"
			),
		):
			validate_auth()
