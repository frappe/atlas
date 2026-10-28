from __future__ import annotations

import frappe
from frappe.model.base_document import get_controller
from frappe.tests import UnitTestCase

from atlas.investigation.core.desk_actions import (
	ALLOWED_ACTIONS,
	get_desk_actions,
	run_desk_action,
	set_record_value,
)


class TestDeskActions(UnitTestCase):
	def test_every_allowed_action_is_a_whitelisted_method(self) -> None:
		for group, actions_by_doctype in ALLOWED_ACTIONS.items():
			for doctype, actions in actions_by_doctype.items():
				for action in actions:
					with self.subTest(group=group, doctype=doctype, action=action):
						self.assertIn(getattr(get_controller(doctype), action, None), frappe.whitelisted)

	def test_lists_actions_with_their_approval(self) -> None:
		actions = {action["action"]: action for action in get_desk_actions("Metal Server")}

		self.assertFalse(actions["sync_state"]["needs_approval"])
		self.assertTrue(actions["upgrade_metald"]["needs_approval"])
		self.assertNotIn("archive_server", actions)

	def test_refuses_blocked_and_framework_actions(self) -> None:
		for doctype, action in (
			("Virtual Machine", "terminate"),
			("Virtual Machine", "set_termination_protection"),
			("Metal Server", "archive_server"),
			("Atlas Settings", "view_proxy_cluster_password"),
			("Virtual Machine", "cancel"),
			("Virtual Machine", "rename"),
			("User", "reset_password"),
		):
			with self.subTest(doctype=doctype, action=action), self.assertRaises(frappe.ValidationError):
				run_desk_action(doctype, "any", action)

	def test_refuses_fields_that_an_agent_cannot_change(self) -> None:
		for fieldname in ("is_termination_protected", "current_state", "missing_field"):
			with self.subTest(fieldname=fieldname), self.assertRaises(frappe.ValidationError):
				set_record_value("Virtual Machine", "any", fieldname, 1)
