from __future__ import annotations

from typing import Annotated

import frappe
from frappe.flow import ToolContext

from atlas.atlas.core.scrub import scrub
from atlas.operations.doctype.investigation.investigation import Investigation
from atlas.operations.investigator.tools.decorators import engineer_tool


@engineer_tool()
def save_investigation(
	problem: Annotated[str, "The problem, as the person reported it."],
	outcome: Annotated[
		str, "What you found: symptom, evidence, cause, confidence, and next step, in Markdown."
	],
	decisions: Annotated[list[str] | None, "Decisions and actions of this chat, one sentence each."] = None,
	context: Annotated[str | None, "JSON with resource names, times, and alerts."] = None,
	*,
	ctx: ToolContext,
) -> dict:
	"""Write the summary of this chat to its Investigation: the problem, the outcome, and the decisions.

	Atlas logs every tool call there already. Call it when the person asks to record the investigation, or
	when the work ends. Calling it again updates the same record.
	"""
	investigation = frappe.get_doc("Investigation", Investigation.get_for_session(ctx.session_id))
	investigation.update_summary({"problem": problem, "outcome": scrub(outcome), "context": context})
	investigation.add_decisions(decisions or [], ctx.user)
	return {"investigation": investigation.name}
