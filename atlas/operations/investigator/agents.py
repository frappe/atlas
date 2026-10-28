from __future__ import annotations

import frappe
from frappe.flow import Agent
from frappe.flow.knowledge import Knowledge

from atlas.operations.doctype.investigation.investigation import INVESTIGATOR_AGENT
from atlas.operations.investigator.tools import bmc, changes, hosts, inventory, records, telemetry

ENGINEER_AGENT = "Atlas Engineer Assistant"
KNOWLEDGE_BASE = "Atlas"
SKILLS = ["incident-investigation", "simplified-technical-english"]
READ_TOOLS = [
	telemetry.list_failing_alert_checks,
	telemetry.get_telemetry_freshness,
	telemetry.list_saved_queries,
	telemetry.run_saved_query,
	telemetry.query_telemetry,
	telemetry.get_counter_increase,
	telemetry.get_reconciler_status,
	inventory.find_resources,
	inventory.describe_host,
	inventory.describe_virtual_machine,
	inventory.list_recent_errors,
	inventory.read_error_log,
	inventory.get_host_changes,
	hosts.read_firecracker_log,
	hosts.read_virtual_machine_journal,
	hosts.read_metald_journal,
	hosts.check_host_health,
	hosts.run_read_only_command,
	bmc.read_redfish_resource,
	records.wait,
]
ENGINEER_TOOLS = [
	*READ_TOOLS,
	changes.run_command,
	changes.list_desk_actions,
	changes.run_desk_action,
	changes.run_dangerous_desk_action,
	changes.set_record_value,
	bmc.run_redfish_request,
	bmc.run_ipmitool,
	records.save_investigation,
]

COMMON_INSTRUCTIONS = """You investigate problems in one Atlas region. Atlas runs Firecracker VMs on Metal hosts.
Load the incident-investigation skill first and follow it. Search the Atlas knowledge base for how a component works.
Prefer Datum telemetry and saved queries. Read a host over SSH only for data that Datum does not have.
Every tool result is scrubbed. Never ask for or print a credential.
Name the evidence for each claim: the tool, the query key or console log, and the time range.
Write every reply in ASD-STE100 Simplified Technical English, as the simplified-technical-english skill describes.
Reply with a short bullet list, no preamble, and no summary. Use a table for 2 or more resources with the same fields.
Show a cause chain as ASCII in a text code block."""

INVESTIGATOR_INSTRUCTIONS = f"""{COMMON_INSTRUCTIONS}
You are read-only. You cannot change anything, and you cannot connect to a VM of a customer tenant.
If the fix needs a change, write the change as a recommendation for an Atlas engineer.
Use at most 30 tool calls. Then write the report that the skill describes with the evidence you have.
If the cause is not certain, give your best hypothesis, set the confidence to low, and list what to check next."""

ENGINEER_INSTRUCTIONS = f"""{COMMON_INSTRUCTIONS}
You help an Atlas engineer. Commands, field changes, and dangerous Desk actions need the engineer's approval in the chat.
Use a change only when the engineer asks for the change, or after you explain why it is needed.
Ask the engineer before any BMC request or ipmitool command that changes state, such as a PATCH, a POST, or a set command.
Find a Desk action with list_desk_actions. Run a normal action with run_desk_action and any other with run_dangerous_desk_action.
Termination, archive, and deletion are not available. Tell the engineer to do them in the Desk.
When the engineer asks, save the chat with save_investigation."""


def get_agents() -> list[Agent]:
	"""The investigation agents for the flow_agents hook. They exist only when Atlas Settings names a model."""
	model = frappe.db.get_single_value("Atlas Settings", "investigation_model")
	if not model:
		return []

	knowledge = Knowledge(KNOWLEDGE_BASE, description="Atlas handbook, specifications, and incident reports.")
	return [
		Agent(
			name=INVESTIGATOR_AGENT,
			model=model,
			description="Read-only investigation of a regional problem. Central starts it.",
			instructions=INVESTIGATOR_INSTRUCTIONS,
			tools=READ_TOOLS,
			knowledge=knowledge,
			skills=SKILLS,
			max_iterations=60,
		),
		Agent(
			name=ENGINEER_AGENT,
			model=model,
			description="Investigate and fix regional problems with an Atlas engineer.",
			instructions=ENGINEER_INSTRUCTIONS,
			tools=ENGINEER_TOOLS,
			knowledge=knowledge,
			skills=SKILLS,
		),
	]


def enable_skill_commands() -> None:
	"""Let engineers start the Atlas skills with /skill:<name>. Flow syncs app skills with the command off."""
	for name in SKILLS:
		if frappe.db.get_value("Flow Skill", name, "user_invocable") == 0:
			skill = frappe.get_doc("Flow Skill", name)
			skill.user_invocable = 1
			skill.save(ignore_permissions=True)
