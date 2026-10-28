from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import frappe
from frappe import _
from frappe.flow import Tool, tool

from atlas.atlas.core.scrub import scrub_value
from atlas.investigation.doctype.investigation.investigation import (
	record_tool_call,
	validate_investigation_enabled,
)

INVESTIGATOR_ROLES = frozenset({"Atlas Investigator", "System Manager"})
ENGINEER_ROLES = frozenset({"System Manager"})
MAXIMUM_RESULT_CHARACTERS = 40_000


def read_tool(*, background: bool = False) -> Callable[[Callable[..., Any]], Tool]:
	"""A read-only tool. It needs the Atlas Investigator role, and its result is scrubbed."""
	return lambda function: tool(_guarded(function, INVESTIGATOR_ROLES, "Tool Call"), background=background)


def engineer_tool(*, background: bool = False) -> Callable[[Callable[..., Any]], Tool]:
	"""A tool for engineers that runs without approval, such as a normal Desk action. It needs System Manager."""
	return lambda function: tool(_guarded(function, ENGINEER_ROLES, "Action"), background=background)


def change_tool(confirm_prompt: Callable[[dict[str, Any]], str]) -> Callable[[Callable[..., Any]], Tool]:
	"""A tool that can change state. It needs System Manager and the approval of the person in the chat."""
	return lambda function: tool(
		_guarded(function, ENGINEER_ROLES, "Action", needs_approval=True),
		requires_confirmation=True,
		confirm_prompt=confirm_prompt,
		background=True,
	)


def _guarded(
	function: Callable[..., Any], roles: frozenset[str], kind: str, needs_approval: bool = False
) -> Callable[..., Any]:
	"""Check the setting and the role, scrub the result, and log the call in the Investigation of the chat."""

	@functools.wraps(function)
	def wrapped(*args: Any, **kwargs: Any) -> Any:
		ctx = kwargs["ctx"]
		arguments = {key: value for key, value in kwargs.items() if key != "ctx"}
		log = functools.partial(record_tool_call, ctx, function.__name__, arguments, kind, needs_approval)
		try:
			validate_investigation_enabled()
			if not roles & set(frappe.get_roles()):
				raise frappe.PermissionError(
					_("{0} needs one of these roles: {1}.").format(
						function.__name__, ", ".join(sorted(roles))
					)
				)
			result = scrub_value(function(*args, **kwargs))
		except Exception as error:
			# Flow rolls back a failed call. Keep only its log entry.
			frappe.db.rollback()
			log(error=error)
			frappe.db.commit()  # nosemgrep: frappe-manual-commit
			raise

		log(result=result)
		text = frappe.as_json(result)
		if len(text) > MAXIMUM_RESULT_CHARACTERS:
			return f"{text[:MAXIMUM_RESULT_CHARACTERS]}\n[Truncated. Ask for a smaller range.]"
		return result

	return wrapped
