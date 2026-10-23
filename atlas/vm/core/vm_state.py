from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import now_datetime

LIVE_STATES = frozenset({"running", "stopped", "paused"})
HOST_REPORT_REPEAT_SECONDS = 3600


def store_reported_states(server_name: str, reported: object) -> None:
	"""Store the reported virtual machine states of one host.

	A new or changed state is saved as a document, so its webhook runs. An unchanged
	state needs only its sync time, so all of them share one update.
	"""
	statuses = get_reported_statuses(reported)
	report_unknown_virtual_machines(server_name, list(statuses))
	names = frappe.get_all("Virtual Machine", filters={"server": server_name}, pluck="name")
	if not names:
		return

	stored = {
		row.name: row
		for row in frappe.get_all(
			"Virtual Machine State", filters={"name": ["in", names]}, fields=["name", "status", "server"]
		)
	}
	synced_at = now_datetime()
	unchanged = []

	for name in names:
		status = statuses.get(name)
		if status is None:
			continue

		row = stored.get(name)
		if row and row.status == status and row.server == server_name:
			unchanged.append(name)
			continue

		try:
			if row:
				state = frappe.get_doc("Virtual Machine State", name)
			else:
				state = frappe.new_doc("Virtual Machine State")
				state.virtual_machine = name

			state.server = server_name
			state.status = status
			state.synced_at = synced_at
			state.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep
		except Exception:
			frappe.db.rollback()
			frappe.log_error(f"Virtual Machine State write failed: {name}")

	if unchanged:
		state_table = frappe.qb.DocType("Virtual Machine State")
		frappe.qb.update(state_table).set(state_table.synced_at, synced_at).where(
			state_table.name.isin(unchanged)
		).run()
		frappe.db.commit()  # nosemgrep


def report_unknown_virtual_machines(server_name: str, reported_names: list[str]) -> None:
	"""Log, once an hour, the VMs a host runs that have no Atlas record."""
	if not reported_names:
		return
	known = set(frappe.get_all("Virtual Machine", filters={"name": ["in", reported_names]}, pluck="name"))
	if unknown := sorted(set(reported_names) - known):
		log_host_report(
			f"atlas:unknown-virtual-machines:{server_name}",
			f"Server {server_name} runs unknown Virtual Machines",
			"Atlas has no Virtual Machine record for: " + ", ".join(unknown),
		)


def report_invalid_virtual_machines(server_name: str, names: list[str]) -> None:
	"""Log, once an hour, the VMs whose records the host cannot read. Metal leaves them
	unmanaged and offers no capacity until an operator repairs the record."""
	if names:
		log_host_report(
			f"atlas:invalid-virtual-machines:{server_name}",
			f"Server {server_name} cannot read Virtual Machine records",
			"Metal cannot read the records of: " + ", ".join(sorted(names)),
		)


def log_host_report(key: str, title: str, message: str) -> None:
	"""Log one host report, then the same report at most once an hour."""
	if frappe.cache.get_value(key):
		return
	frappe.cache.set_value(key, 1, expires_in_sec=HOST_REPORT_REPEAT_SECONDS)
	frappe.log_error(title=title, message=message)


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


def has_live_virtual_machine_for_image(image_name: str) -> bool:
	"""Return whether a virtual machine that still needs image_name is alive.

	A draft counts, because Metal can still pull the artifacts for it. Every other
	virtual machine counts only while its host reports a live state, so a virtual
	machine with no reported state does not hold the image.
	"""
	virtual_machine = frappe.qb.DocType("Virtual Machine")
	state = frappe.qb.DocType("Virtual Machine State")

	query = (
		frappe.qb.from_(virtual_machine)
		.left_join(state)
		.on(state.name == virtual_machine.name)
		.select(virtual_machine.name)
		.where(
			(virtual_machine.virtual_machine_image == image_name)
			& (virtual_machine.is_terminating == 0)
			& ((virtual_machine.is_draft == 1) | state.status.isin(sorted(LIVE_STATES)))
		)
		.limit(1)
	)
	return bool(query.run())
