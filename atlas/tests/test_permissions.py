"""Permission hardening tests.

Atlas is single-role: `System Manager` reads/writes everything. Anything
else is denied. These tests pin that contract so a future PR that adds a
new DocType or relaxes a perms block can't silently widen access.
"""

import frappe
from frappe.tests import IntegrationTestCase

from atlas.tests.fixtures import make_provider

PROVIDER_NAME = "atlas-perm-test-provider"
BASIC_USER_EMAIL = "atlas-perm-basic@example.com"
SYSMGR_USER_EMAIL = "atlas-perm-sysmgr@example.com"


def _ensure_system_manager_user() -> str:
	if frappe.db.exists("User", SYSMGR_USER_EMAIL):
		user = frappe.get_doc("User", SYSMGR_USER_EMAIL)
	else:
		user = frappe.get_doc({
			"doctype": "User",
			"email": SYSMGR_USER_EMAIL,
			"first_name": "Sys",
			"last_name": "Mgr",
			"send_welcome_email": 0,
			"enabled": 1,
			"roles": [{"role": "System Manager"}],
		}).insert(ignore_permissions=True)
	role_names = {row.role for row in (user.get("roles") or [])}
	if "System Manager" not in role_names:
		user.append("roles", {"role": "System Manager"})
		user.save(ignore_permissions=True)
	return user.name


def _make_basic_user() -> str:
	if frappe.db.exists("User", BASIC_USER_EMAIL):
		user = frappe.get_doc("User", BASIC_USER_EMAIL)
	else:
		user = frappe.get_doc({
			"doctype": "User",
			"email": BASIC_USER_EMAIL,
			"first_name": "Perm",
			"last_name": "Test",
			"send_welcome_email": 0,
			"enabled": 1,
		}).insert(ignore_permissions=True)
	# Strip everything: no System Manager, no nothing.
	for role_row in list(user.get("roles") or []):
		user.remove(role_row)
	user.save(ignore_permissions=True)
	return user.name


class TestPermissions(IntegrationTestCase):
	def setUp(self) -> None:
		self.provider = make_provider(PROVIDER_NAME)
		self.basic_user = _make_basic_user()
		self.addCleanup(frappe.set_user, "Administrator")

	def test_only_system_manager_can_read_provider(self) -> None:
		frappe.set_user(self.basic_user)
		self.assertFalse(
			frappe.has_permission("Provider", "read", doc=self.provider.name),
			"basic user must not be able to read Provider",
		)

	def test_api_token_not_in_get_doc_response(self) -> None:
		# api_token now lives on DigitalOcean Settings, not on the Provider
		# row. Password fields land in the auth table; a fresh get_doc must
		# not surface plaintext.
		import frappe.utils.password
		frappe.utils.password.set_encrypted_password(
			"DigitalOcean Settings", "DigitalOcean Settings",
			"dop_v1_perm_test", "api_token",
		)
		doc = frappe.get_single("DigitalOcean Settings")
		serialized = doc.as_dict()
		self.assertNotIn("dop_v1_perm_test", str(serialized))
		self.assertNotEqual(serialized.get("api_token"), "dop_v1_perm_test")

	def test_task_delete_blocked_by_perms(self) -> None:
		import json

		task = frappe.get_doc({
			"doctype": "Task",
			"script": "noop.sh",
			"variables": json.dumps({}),
			"status": "Pending",
			"triggered_by": "Administrator",
		}).insert(ignore_permissions=True)

		# System Manager has delete = 0 on Task. The permission check itself
		# is the contract — Administrator bypasses checks, so we assert the
		# permission state directly rather than calling delete_doc.
		sysmgr = _ensure_system_manager_user()
		frappe.set_user(sysmgr)
		try:
			self.assertFalse(
				frappe.has_permission("Task", "delete", doc=task.name),
				"System Manager must not be able to delete Task rows (audit log)",
			)
		finally:
			frappe.set_user("Administrator")
