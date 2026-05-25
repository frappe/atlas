"""Permission hardening tests.

Atlas is single-role: `System Manager` reads/writes everything. Anything
else is denied. These tests pin that contract so a future PR that adds a
new DocType or relaxes a perms block can't silently widen access.
"""

import frappe
from frappe.tests import IntegrationTestCase

PROVIDER_NAME = "atlas-perm-test-provider"
BASIC_USER_EMAIL = "atlas-perm-basic@example.com"
SYSMGR_USER_EMAIL = "atlas-perm-sysmgr@example.com"
PRIVATE_KEY_PLAINTEXT = "-----BEGIN OPENSSH PRIVATE KEY-----\nperm-test-secret\n"


def _make_provider() -> "frappe.model.document.Document":
	if frappe.db.exists("Server Provider", PROVIDER_NAME):
		return frappe.get_doc("Server Provider", PROVIDER_NAME)
	return frappe.get_doc({
		"doctype": "Server Provider",
		"provider_name": PROVIDER_NAME,
		"provider_type": "DigitalOcean",
		"api_token": "dop_v1_perm_test",
		"ssh_key_id": "fp:perm-test",
		"ssh_private_key": PRIVATE_KEY_PLAINTEXT,
		"default_region": "blr1",
		"default_size": "s-2vcpu-4gb-intel",
		"default_image": "ubuntu-24-04-x64",
		"is_active": 1,
	}).insert(ignore_permissions=True)


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
		self.provider = _make_provider()
		self.basic_user = _make_basic_user()
		self.addCleanup(frappe.set_user, "Administrator")

	def test_only_system_manager_can_read_server_provider(self) -> None:
		frappe.set_user(self.basic_user)
		self.assertFalse(
			frappe.has_permission("Server Provider", "read", doc=self.provider.name),
			"basic user must not be able to read Server Provider",
		)

	def test_ssh_private_key_not_in_get_doc_response(self) -> None:
		# Password fields are stored in the auth table, not on the row.
		# A fresh get_doc must not surface plaintext.
		doc = frappe.get_doc("Server Provider", self.provider.name)
		serialized = doc.as_dict()
		self.assertNotIn(PRIVATE_KEY_PLAINTEXT, str(serialized))
		self.assertNotEqual(serialized.get("ssh_private_key"), PRIVATE_KEY_PLAINTEXT)

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
