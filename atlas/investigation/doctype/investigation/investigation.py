# Copyright (c) 2026, Frappe and contributors
# For license information, please see license.txt

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, now_datetime

from atlas.atlas.core.scrub import scrub, scrub_value
from atlas.investigation.doctype.investigation_entry.investigation_entry import InvestigationEntry

if TYPE_CHECKING:
	from frappe.flow import ToolContext
	from frappe.flow.doctype.flow_run.flow_run import FlowRun
	from frappe.flow.doctype.flow_session.flow_session import FlowSession

INVESTIGATOR_AGENT = "Atlas Investigator"
INVESTIGATOR_USER = "investigator@atlas.internal"
INVESTIGATOR_ROLES = ("Atlas Investigator", "Flow User")
ON_FLOW_RUN_COMPLETE = "atlas.investigation.doctype.investigation.investigation.record_flow_run"
ITERATION_LIMIT_ERROR = "The agent hit its iteration limit."
REFERENCE_KEYS = {"console_log": "Metal Server Console Log", "ssh_task": "SSH Task"}
# A paused run waits for approval in the chat, and a denied call ends the run normally.
STATUS_OF_FLOW_RUN = {
	"Running": "Running", "Paused": "Running", "Completed": "Completed", "Denied": "Completed",
	"Failed": "Failed",
}  # fmt: skip
MAXIMUM_TITLE_LENGTH = 140
IDLE_HOURS = 3
IDLE_ERROR = f"No activity for {IDLE_HOURS} hours."
MAXIMUM_DETAIL_CHARACTERS = 8_000
REPORT_REQUEST = (
	"Stop investigating. Write the report now, in the format of the skill, from the evidence you have."
)


class Investigation(Document):
	"""One problem, its context, its outcome, and the log of how it was found.

	Central creates one through the API, and the read-only investigator works on it in the background.
	Every other chat with an Atlas agent gets one at its first tool call. Atlas logs each tool call in it.
	"""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from atlas.investigation.doctype.investigation_entry.investigation_entry import InvestigationEntry

		context: DF.Code | None
		entries: DF.Table[InvestigationEntry]
		error: DF.SmallText | None
		flow_session: DF.Link | None
		outcome: DF.MarkdownEditor | None
		problem: DF.SmallText
		requested_by: DF.Data | None
		source: DF.Literal["Central", "Engineer"]
		status: DF.Literal["Queued", "Running", "Completed", "Failed"]
		title: DF.Data
	# end: auto-generated types

	@staticmethod
	def create_for_central(
		problem: str, title: str | None, context: dict | None, requested_by: str | None
	) -> Investigation:
		"""Record a problem from Central. The investigator starts in a background job."""
		validate_investigation_enabled()
		return frappe.get_doc(
			{
				"doctype": "Investigation",
				"source": "Central",
				"title": title or problem.strip().splitlines()[0][:MAXIMUM_TITLE_LENGTH],
				"problem": problem,
				"context": json.dumps(context) if context else None,
				"requested_by": requested_by,
			}
		).insert(ignore_permissions=True)

	@staticmethod
	def get_for_session(session: str) -> str:
		"""Return the Investigation of a Flow Session. Create it at the first tool call of an engineer chat."""
		if name := frappe.db.get_value("Investigation", {"flow_session": session}):
			return name

		chat = frappe.db.get_value("Flow Session", session, ["title", "owner"], as_dict=True)
		if not chat:
			frappe.throw(_("Flow Session {0} does not exist.").format(session))
		message = frappe.db.get_value("Flow Run", {"session": session}, "input", order_by="creation asc")
		problem = scrub(message or chat.title or session)
		title = chat.title or problem.strip().splitlines()[0][:MAXIMUM_TITLE_LENGTH]
		frappe.db.savepoint("investigation_for_session")
		try:
			return (
				frappe.get_doc(
					{
						"doctype": "Investigation",
						"source": "Engineer",
						"status": "Running",
						"title": title,
						"problem": problem,
						"requested_by": chat.owner,
						"flow_session": session,
					}
				)
				.insert(ignore_permissions=True)
				.name
			)
		except frappe.UniqueValidationError:
			# A parallel tool call of the same chat created it first.
			frappe.db.rollback(save_point="investigation_for_session")
			return frappe.db.get_value("Investigation", {"flow_session": session})

	def validate(self) -> None:
		if self.context:
			try:
				json.loads(self.context)
			except json.JSONDecodeError:
				frappe.throw(_("Context must be JSON."))

	def after_insert(self) -> None:
		if self.source == "Central":
			frappe.enqueue_doc(
				self.doctype,
				self.name,
				"start_investigator",
				queue="long",
				job_id=f"atlas||investigation||{self.name}",
				deduplicate=True,
				enqueue_after_commit=True,
			)

	def start_investigator(self) -> None:
		"""Start the read-only investigator on the problem, as the investigator user."""
		from frappe.flow.lib.session import new_session

		previous_user = frappe.session.user
		frappe.set_user(ensure_investigator_user())  # nosemgrep
		try:
			session = new_session(INVESTIGATOR_AGENT, title=self.title)
			# Commit the link first: a short run can end, and look for it, before chat returns.
			self.db_set({"status": "Running", "flow_session": session.name}, commit=True)
			session.chat(self.prompt, on_complete=ON_FLOW_RUN_COMPLETE)
		except Exception as error:
			self.db_set({"status": "Failed", "error": str(error)[:1000]}, commit=True)
			raise
		finally:
			frappe.set_user(previous_user)  # nosemgrep

	@property
	def prompt(self) -> str:
		"""The first message to the investigator."""
		context = f"\n\nContext:\n{self.context}" if self.context else ""
		return f"Investigate this problem and report the cause.\n\nProblem:\n{self.problem}{context}"

	def record_run(self, run: FlowRun) -> None:
		"""Store the outcome of an ended investigator run.

		A first run that reaches the iteration limit gets one more turn to write its report.
		"""
		if run.error == ITERATION_LIMIT_ERROR and frappe.db.count("Flow Run", {"session": run.session}) == 1:
			frappe.get_doc("Flow Session", run.session).chat(REPORT_REQUEST, on_complete=ON_FLOW_RUN_COMPLETE)
			return

		self.db_set(
			{
				"status": "Completed" if run.status == "Completed" else "Failed",
				"outcome": scrub(run.output or ""),
				"error": run.error,
			}
		)

	def update_summary(self, values: dict) -> None:
		"""Change the problem, outcome, status, or context, and leave the log as it is."""
		self.update(values)
		self.validate()
		self.db_set(values)

	def add_decisions(self, decisions: list[str], user: str) -> None:
		"""Append decisions to the log."""
		for decision in decisions:
			InvestigationEntry.append_to(
				self.name, kind="Decision", title=decision, detail=scrub(decision), user=user
			)


def validate_investigation_enabled() -> None:
	"""Refuse while Atlas Settings turns investigation off."""
	if not frappe.db.get_single_value("Atlas Settings", "is_investigation_enabled"):
		frappe.throw(_("Investigation is off. Enable it in Atlas Settings."))


def record_tool_call(
	ctx: ToolContext,
	tool: str,
	arguments: dict,
	kind: str,
	needs_approval: bool,
	result: object = None,
	error: Exception | None = None,
) -> None:
	"""Log one tool call in the Investigation of its chat, with the record that it ran or acted on."""
	if error is None:
		status = "Success"
	elif isinstance(error, frappe.ValidationError | frappe.PermissionError):
		status = "Refused"
	else:
		status = "Failed"

	arguments = scrub_value(arguments)
	outcome = {"error": scrub(str(error))} if error else {"result": result}
	reference_doctype, reference_name = _get_reference(result)
	is_auto_approved = frappe.db.get_value("Flow Session", ctx.session_id, "auto_approve")
	InvestigationEntry.append_to(
		Investigation.get_for_session(ctx.session_id),
		kind=kind,
		title=f"{tool} {', '.join(str(value) for value in arguments.values())}",
		status=status,
		user=ctx.user,
		is_approved=needs_approval and not is_auto_approved,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		detail=frappe.as_json({"arguments": arguments, **outcome})[:MAXIMUM_DETAIL_CHARACTERS],
	)


def _get_reference(result: object) -> tuple[str | None, str | None]:
	"""The console log or SSH Task of a result, else the record that a Desk action acted on."""
	if not isinstance(result, dict):
		return None, None
	for key, doctype in REFERENCE_KEYS.items():
		if result.get(key):
			return doctype, result[key]
	if isinstance(result.get("result"), str) and frappe.db.exists("SSH Task", result["result"]):
		return "SSH Task", result["result"]
	return result.get("doctype"), result.get("name")


def sync_with_flow_session(session: FlowSession, method: str | None = None) -> None:
	"""Follow the chat on each Flow sync: the status of its latest run, and the title of an engineer chat."""
	investigation = frappe.db.get_value(
		"Investigation", {"flow_session": session.name}, ["name", "source", "status", "title"], as_dict=True
	)
	if not investigation:
		return

	values = {}
	run_status = frappe.db.get_value(
		"Flow Run", {"session": session.name}, "status", order_by="creation desc"
	)
	if run_status and STATUS_OF_FLOW_RUN[run_status] != investigation.status:
		values["status"] = STATUS_OF_FLOW_RUN[run_status]
		if values["status"] == "Running":
			values["error"] = None
	if investigation.source == "Engineer" and session.title and session.title != investigation.title:
		values["title"] = session.title[:MAXIMUM_TITLE_LENGTH]
	if values:
		frappe.db.set_value("Investigation", investigation.name, values)


def fail_idle_investigations() -> None:
	"""Mark a queued or running Investigation as Failed when it and its chat had no activity for IDLE_HOURS."""
	cutoff = add_to_date(now_datetime(), hours=-IDLE_HOURS)
	investigation = frappe.qb.DocType("Investigation")
	session = frappe.qb.DocType("Flow Session")
	names = (
		frappe.qb.from_(investigation)
		.left_join(session)
		.on(session.name == investigation.flow_session)
		.select(investigation.name)
		.where(investigation.status.isin(["Queued", "Running"]))
		.where(investigation.modified < cutoff)
		.where(session.modified.isnull() | (session.modified < cutoff))
	).run(pluck=True)
	for name in names:
		frappe.db.set_value("Investigation", name, {"status": "Failed", "error": IDLE_ERROR})


def record_flow_run(run: FlowRun) -> None:
	"""Flow calls this when an investigator run ends."""
	name = frappe.db.get_value("Investigation", {"flow_session": run.session})
	if name:
		frappe.get_doc("Investigation", name).record_run(run)


def ensure_investigator_user() -> str:
	"""Return the user that runs Central investigations, with only the investigator roles."""
	if not frappe.db.exists("User", INVESTIGATOR_USER):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": INVESTIGATOR_USER,
				"first_name": "Atlas Investigator",
				"user_type": "System User",
				"send_welcome_email": 0,
			}
		)
		user.flags.no_welcome_mail = True
		user.insert(ignore_permissions=True)
		user.add_roles(*INVESTIGATOR_ROLES)
	return INVESTIGATOR_USER
