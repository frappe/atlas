from __future__ import annotations

import frappe
from frappe.flow import ToolContext
from frappe.tests import IntegrationTestCase

from atlas.investigation.doctype.investigation.investigation import (
	Investigation,
	fail_idle_investigations,
	record_tool_call,
	sync_with_flow_session,
)
from atlas.investigation.doctype.investigation_entry.investigation_entry import InvestigationEntry


class TestInvestigation(IntegrationTestCase):
	def setUp(self) -> None:
		session = frappe.get_doc({"doctype": "Flow Session", "agent": "Flow", "title": "Disk alert on waw-1"})
		session.flags.ignore_validate = session.flags.ignore_links = True
		session.insert(ignore_permissions=True)
		self.session = session
		self.context = ToolContext(session_id=session.name, agent="Flow", user="Administrator")

	def test_a_chat_gets_one_investigation(self) -> None:
		name = Investigation.get_for_session(self.context.session_id)

		self.assertEqual(Investigation.get_for_session(self.context.session_id), name)
		self.assertEqual(
			frappe.db.get_value("Investigation", name, ["source", "title"]),
			("Engineer", "Disk alert on waw-1"),
		)

	def test_logs_a_tool_call_with_its_record_as_text(self) -> None:
		record_tool_call(
			self.context,
			"read_firecracker_log",
			{"virtual_machine": "vm-1", "token": "Bearer abcdefghijklmnopqrstuvwxyz0123456789"},
			"Tool Call",
			needs_approval=False,
			result={"console_log": "metal-server-console-log-00001", "output": "ok"},
		)
		record_tool_call(
			self.context,
			"run_command",
			{"target": "waw-1", "command": "systemctl restart metal"},
			"Action",
			needs_approval=True,
			error=frappe.ValidationError("refused"),
		)

		entries = self._entries()
		self.assertEqual(
			[
				(entry.status, entry.reference_doctype, entry.reference_name, entry.is_approved)
				for entry in entries
			],
			[
				("Success", "Metal Server Console Log", "metal-server-console-log-00001", 0),
				("Refused", None, None, 1),
			],
		)
		self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123456789", entries[0].detail)

	def test_a_summary_change_keeps_rows_that_others_inserted(self) -> None:
		name = Investigation.get_for_session(self.context.session_id)
		stale = frappe.get_doc("Investigation", name)
		InvestigationEntry.append_to(name, kind="Note", title="inserted by a parallel tool call")

		stale.update_summary({"outcome": "Disk full", "status": "Completed"})

		self.assertEqual([entry.title for entry in self._entries()], ["inserted by a parallel tool call"])
		self.assertEqual(frappe.db.get_value("Investigation", name, "status"), "Completed")

	def test_refuses_a_central_investigation_while_investigation_is_off(self) -> None:
		frappe.db.set_single_value("Atlas Settings", "is_investigation_enabled", 0)

		with self.assertRaises(frappe.ValidationError):
			Investigation.create_for_central("Disk alert on waw-1", None, None, None)

	def test_follows_the_status_of_the_latest_run_and_the_chat_title(self) -> None:
		name = Investigation.get_for_session(self.context.session_id)
		for status, expected in (("Paused", "Running"), ("Completed", "Completed"), ("Running", "Running")):
			run = frappe.get_doc({"doctype": "Flow Run", "session": self.session.name, "status": status})
			run.flags.ignore_links = run.flags.ignore_validate = True
			run.insert(ignore_permissions=True)
			self.session.title = f"Disk alert on waw-1, {status}"

			sync_with_flow_session(self.session)

			with self.subTest(status=status):
				self.assertEqual(
					frappe.db.get_value("Investigation", name, ["status", "title"]),
					(expected, self.session.title),
				)

	def test_fails_an_investigation_without_activity_for_three_hours(self) -> None:
		name = Investigation.get_for_session(self.context.session_id)
		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-4)
		for doctype, record in (("Investigation", name), ("Flow Session", self.session.name)):
			frappe.db.set_value(doctype, record, "modified", old, update_modified=False)

		fail_idle_investigations()

		self.assertEqual(frappe.db.get_value("Investigation", name, "status"), "Failed")

	def _entries(self) -> list:
		name = Investigation.get_for_session(self.context.session_id)
		return frappe.get_doc("Investigation", name).entries
