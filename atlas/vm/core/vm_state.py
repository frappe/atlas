from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import now_datetime


def store_reported_states(server_name: str, reported: object) -> None:
	"""Store the reported virtual machine states of one host."""
	statuses = get_reported_statuses(reported)
	names = frappe.get_all("Virtual Machine", filters={"server": server_name}, pluck="name")
	if not names:
		return

	stored = set(frappe.get_all("Virtual Machine State", filters={"name": ["in", names]}, pluck="name"))
	synced_at = now_datetime()

	for name in names:
		status = statuses.get(name)
		if status is None:
			continue

		try:
			if name in stored:
				state = frappe.get_doc("Virtual Machine State", name)
			else:
				state = frappe.new_doc("Virtual Machine State")
				state.virtual_machine = name

			state.status = status
			state.synced_at = synced_at
			state.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep
		except Exception:
			frappe.log_error(f"Virtual Machine State write failed: {name}")


def get_reported_statuses(reported: object) -> dict[str, str]:
	"""Return the status of each virtual machine in a Metal sync response."""
	if not isinstance(reported, dict):
		raise ValueError("Metal virtual machine response must be an object")

	statuses = {}
	for name, state in reported.items():
		status = state.get("status") if isinstance(state, dict) else None
		if not isinstance(status, str) or not status:
			raise ValueError("Metal virtual machine response has invalid values")
		statuses[name] = status
	return statuses


def get_reported_state_rows(names: list[str]) -> dict[str, Any]:
	"""Return the stored state row of each named virtual machine."""
	if not names:
		return {}

	rows = frappe.get_all(
		"Virtual Machine State",
		filters={"name": ["in", names]},
		fields=["name", "status", "synced_at"],
	)
	return {row.name: row for row in rows}
