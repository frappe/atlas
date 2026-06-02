"""Permission contract tests.

Atlas has two audiences (see spec/11-user-ui.md):

- **Operators** are `System Manager`. They read/write the whole fleet.
- **Users** hold the `Atlas User` role. They see only their own Virtual
  Machines and Snapshots, read the shared base Images, and — for the inline
  Activity panel — only the Tasks of a machine they own. They have no access
  to Provider, Server, or the global Task log.

These tests pin both halves so a future PR that adds a DocType or relaxes a
perms block can't silently widen access. The row-level scoping is enforced by
`atlas/atlas/permissions.py` (query conditions + the Task has_permission hook)
wired in `hooks.py`.
"""

import json

import frappe
from frappe.tests import IntegrationTestCase

from atlas.tests.fixtures import (
	make_image,
	make_provider,
	make_server,
	make_virtual_machine,
)

PROVIDER_NAME = "atlas-perm-test-provider"
BASIC_USER_EMAIL = "atlas-perm-basic@example.com"
SYSMGR_USER_EMAIL = "atlas-perm-sysmgr@example.com"
USER_A_EMAIL = "atlas-perm-user-a@example.com"
USER_B_EMAIL = "atlas-perm-user-b@example.com"


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


def _make_atlas_user(email: str) -> str:
	"""A user holding only the `Atlas User` role — the SPA's audience."""
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
	else:
		user = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": "Atlas",
			"last_name": "User",
			"send_welcome_email": 0,
			"enabled": 1,
		}).insert(ignore_permissions=True)
	for role_row in list(user.get("roles") or []):
		user.remove(role_row)
	user.append("roles", {"role": "Atlas User"})
	user.save(ignore_permissions=True)
	return user.name


def _ensure_atlas_user_role() -> None:
	if not frappe.db.exists("Role", "Atlas User"):
		frappe.get_doc({
			"doctype": "Role",
			"role_name": "Atlas User",
			"desk_access": 0,
		}).insert(ignore_permissions=True)


class TestPermissions(IntegrationTestCase):
	def setUp(self) -> None:
		_ensure_atlas_user_role()
		self.provider = make_provider(PROVIDER_NAME)
		self.basic_user = _make_basic_user()
		self.addCleanup(frappe.set_user, "Administrator")

	# ----- operator-side contract (unchanged) -----------------------------

	def test_only_system_manager_can_read_provider(self) -> None:
		frappe.set_user(self.basic_user)
		self.assertFalse(
			frappe.has_permission("Provider", "read", doc=self.provider.name),
			"basic user must not be able to read Provider",
		)

	def test_api_token_not_in_get_doc_response(self) -> None:
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
		task = frappe.get_doc({
			"doctype": "Task",
			"script": "noop.sh",
			"variables": json.dumps({}),
			"status": "Pending",
			"triggered_by": "Administrator",
		}).insert(ignore_permissions=True)

		sysmgr = _ensure_system_manager_user()
		frappe.set_user(sysmgr)
		try:
			self.assertFalse(
				frappe.has_permission("Task", "delete", doc=task.name),
				"System Manager must not be able to delete Task rows (audit log)",
			)
		finally:
			frappe.set_user("Administrator")

	# ----- Atlas User contract (the SPA's audience) -----------------------

	def _seed_vm(self, owner_email: str, title: str):
		"""Insert a VM owned by `owner_email` (Frappe stamps `owner` from the
		acting user). server/image are pre-filled so the placement defaults
		don't run inside a perms test."""
		server = make_server("atlas-perm-server", provider=self.provider.name)
		image = make_image("atlas-perm-image")
		previous = frappe.session.user
		frappe.set_user(owner_email)
		try:
			vm = frappe.get_doc({
				"doctype": "Virtual Machine",
				"title": title,
				"server": server.name,
				"image": image.name,
				"vcpus": 1,
				"memory_megabytes": 512,
				"disk_gigabytes": 2,
				"ssh_public_key": "ssh-ed25519 AAAA",
			}).insert()
		finally:
			frappe.set_user(previous)
		return vm

	def test_atlas_user_reads_own_vm_not_others(self) -> None:
		user_a = _make_atlas_user(USER_A_EMAIL)
		user_b = _make_atlas_user(USER_B_EMAIL)
		vm_a = self._seed_vm(user_a, "a-machine")

		frappe.set_user(user_a)
		self.assertTrue(
			frappe.has_permission("Virtual Machine", "read", doc=vm_a.name),
			"owner must read their own VM",
		)
		frappe.set_user(user_b)
		self.assertFalse(
			frappe.has_permission("Virtual Machine", "read", doc=vm_a.name),
			"a different Atlas User must not read someone else's VM",
		)

	def test_atlas_user_list_is_owner_scoped(self) -> None:
		user_a = _make_atlas_user(USER_A_EMAIL)
		user_b = _make_atlas_user(USER_B_EMAIL)
		vm_a = self._seed_vm(user_a, "a-only")

		frappe.set_user(user_b)
		names = {row.name for row in frappe.get_list("Virtual Machine", limit_page_length=0)}
		self.assertNotIn(vm_a.name, names, "user B's list must not include user A's VM")

		frappe.set_user(user_a)
		names = {row.name for row in frappe.get_list("Virtual Machine", limit_page_length=0)}
		self.assertIn(vm_a.name, names, "user A's list must include their own VM")

	def test_atlas_user_denied_provider_and_server(self) -> None:
		user_a = _make_atlas_user(USER_A_EMAIL)
		server = make_server("atlas-perm-server", provider=self.provider.name)
		frappe.set_user(user_a)
		self.assertFalse(
			frappe.has_permission("Provider", "read", doc=self.provider.name),
			"Atlas User must not read Provider",
		)
		self.assertFalse(
			frappe.has_permission("Server", "read", doc=server.name),
			"Atlas User must not read Server",
		)

	def test_atlas_user_reads_shared_images(self) -> None:
		user_a = _make_atlas_user(USER_A_EMAIL)
		image = make_image("atlas-perm-image")
		frappe.set_user(user_a)
		self.assertTrue(
			frappe.has_permission("Virtual Machine Image", "read", doc=image.name),
			"images are shared — any Atlas User reads them",
		)
		self.assertFalse(
			frappe.has_permission("Virtual Machine Image", "write", doc=image.name),
			"Atlas User must not write images",
		)

	def test_atlas_user_reads_own_vm_tasks_not_others(self) -> None:
		user_a = _make_atlas_user(USER_A_EMAIL)
		user_b = _make_atlas_user(USER_B_EMAIL)
		vm_a = self._seed_vm(user_a, "a-with-task")

		task_a = frappe.get_doc({
			"doctype": "Task",
			"script": "start-vm.sh",
			"variables": json.dumps({}),
			"status": "Success",
			"virtual_machine": vm_a.name,
			"server": vm_a.server,
			"triggered_by": "Administrator",
		}).insert(ignore_permissions=True)

		frappe.set_user(user_a)
		self.assertTrue(
			frappe.has_permission("Task", "read", doc=task_a.name),
			"owner of the VM must read its Task (inline Activity)",
		)
		frappe.set_user(user_b)
		self.assertFalse(
			frappe.has_permission("Task", "read", doc=task_a.name),
			"a different Atlas User must not read another VM's Task",
		)

	def test_atlas_user_global_task_list_is_scoped(self) -> None:
		user_a = _make_atlas_user(USER_A_EMAIL)
		user_b = _make_atlas_user(USER_B_EMAIL)
		vm_a = self._seed_vm(user_a, "a-task-list")
		task_a = frappe.get_doc({
			"doctype": "Task",
			"script": "start-vm.sh",
			"variables": json.dumps({}),
			"status": "Success",
			"virtual_machine": vm_a.name,
			"server": vm_a.server,
			"triggered_by": "Administrator",
		}).insert(ignore_permissions=True)

		# A user hand-calling get_list('Task') with no filter must see only
		# their owned VMs' tasks — never the fleet's task log.
		frappe.set_user(user_b)
		names = {row.name for row in frappe.get_list("Task", limit_page_length=0)}
		self.assertNotIn(task_a.name, names, "user B must not see user A's VM tasks")

	def test_operator_sees_everything(self) -> None:
		user_a = _make_atlas_user(USER_A_EMAIL)
		vm_a = self._seed_vm(user_a, "operator-visible")
		sysmgr = _ensure_system_manager_user()
		frappe.set_user(sysmgr)
		self.assertTrue(
			frappe.has_permission("Virtual Machine", "read", doc=vm_a.name),
			"operator must read any VM regardless of owner",
		)
		names = {row.name for row in frappe.get_list("Virtual Machine", limit_page_length=0)}
		self.assertIn(vm_a.name, names, "operator's list is unrestricted")
