from __future__ import annotations

from typing import Annotated, Any

import frappe
from frappe import _
from frappe.flow import ToolContext

from atlas.operations.investigator import desk_actions
from atlas.operations.investigator.tools.decorators import change_tool, engineer_tool
from atlas.operations.investigator.tools.hosts import run_on_target
from atlas.operations.investigator.tools.inventory import get_host_name


@change_tool(lambda arguments: f"Run as root on {arguments['target']}:\n{arguments['command']}")
def run_command(
	target: Annotated[str, "Metal Server name or title, or the name of a tenant 0 VM."],
	command: Annotated[str, "Shell command. It can change the host."],
	*,
	ctx: ToolContext,
) -> dict:
	"""Run any command as root on a host or a tenant 0 VM. The person in the chat must approve it."""
	return run_on_target(target, command)


@engineer_tool()
def list_desk_actions(
	doctype: Annotated[str, "Atlas DocType, such as Metal Server, Virtual Machine, or Metald Update."],
	*,
	ctx: ToolContext,
) -> list[dict]:
	"""List the Desk actions that run_desk_action can run on a record of this DocType, with their parameters."""
	return desk_actions.get_desk_actions(doctype)


@engineer_tool(background=True)
def run_desk_action(
	doctype: Annotated[str, "Atlas DocType."],
	name: Annotated[str, "Record name. A Metal Server title also works."],
	action: Annotated[str, "Action from list_desk_actions with needs_approval false."],
	arguments: Annotated[dict[str, Any] | None, "Arguments of the action, by parameter name."] = None,
	*,
	ctx: ToolContext,
) -> dict:
	"""Run one normal Desk action from the Actions or Diagnostics buttons. It needs no approval.
	For a Dangerous Action, an action without a button, or any action on a customer VM, use run_dangerous_desk_action."""
	if doctype == "Metal Server":
		name = get_host_name(name)
	if not desk_actions.is_normal_action(doctype, name, action):
		frappe.throw(
			_("{0} on {1} {2} needs approval. Use run_dangerous_desk_action.").format(action, doctype, name)
		)
	result = desk_actions.run_desk_action(doctype, name, action, arguments)
	return {"doctype": doctype, "name": name, "action": action, "result": result}


@change_tool(
	lambda arguments: (
		f"Run {arguments['action']} on {arguments['doctype']} {arguments['name']}"
		f" with {arguments.get('arguments') or 'no arguments'}"
	)
)
def run_dangerous_desk_action(
	doctype: Annotated[str, "Atlas DocType."],
	name: Annotated[str, "Record name. A Metal Server title also works."],
	action: Annotated[str, "Action from list_desk_actions."],
	arguments: Annotated[dict[str, Any] | None, "Arguments of the action, by parameter name."] = None,
	*,
	ctx: ToolContext,
) -> dict:
	"""Run any Desk action that an agent may run, such as Upgrade Metald or Drain. The person in the chat must approve it."""
	if doctype == "Metal Server":
		name = get_host_name(name)
	result = desk_actions.run_desk_action(doctype, name, action, arguments)
	return {"doctype": doctype, "name": name, "action": action, "result": result}


@change_tool(
	lambda arguments: (
		f"Set {arguments['fieldname']} of {arguments['doctype']} {arguments['name']} to {arguments['value']!r}"
	)
)
def set_record_value(
	doctype: Annotated[str, "Atlas DocType."],
	name: Annotated[str, "Record name. A Metal Server title also works."],
	fieldname: Annotated[str, "Field to change, such as is_in_maintenance."],
	value: Annotated[Any, "New value. Use 0 or 1 for a check box."],
	*,
	ctx: ToolContext,
) -> dict:
	"""Change one editable field of a record and save it, as in the form. The person in the chat must approve it."""
	if doctype == "Metal Server":
		name = get_host_name(name)
	return desk_actions.set_record_value(doctype, name, fieldname, value)
