from __future__ import annotations

from datetime import datetime
from typing import Any

import frappe
from frappe.query_builder import Case
from frappe.utils import now_datetime

from atlas.vm.core.metal_models import timestamp_field

LIVE_STATES = frozenset({"running", "stopped", "paused"})
SLEEP_STATES = ("sleeping", "deep-sleeping", "comatose")
# Metal owns these values. Atlas owns the signed URL fields on the same row.
REPORTED_FIELDS = ("sleep_state", "sleep_snapshot_id", "last_request_at")
HOST_REPORT_REPEAT_SECONDS = 3600


def store_reported_states(server_name: str, reported: object) -> None:
	"""Store the reported virtual machine states of one host.

	A new or changed status is saved as a document, so its webhook runs. The other
	reported values change often, so all rows with an unchanged status share one update.
	"""
	states = get_reported_states(reported)
	report_unknown_virtual_machines(server_name, list(states))
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
	unchanged = {}

	for name in names:
		state = states.get(name)
		if state is None:
			continue

		row = stored.get(name)
		if row and row.status == state.status and row.server == server_name:
			unchanged[name] = state
			continue

		try:
			if row:
				document = frappe.get_doc("Virtual Machine State", name)
			else:
				document = frappe.new_doc("Virtual Machine State")
				document.virtual_machine = name

			document.update(state)
			document.server = server_name
			document.synced_at = synced_at
			document.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep
		except Exception:
			frappe.db.rollback()
			frappe.log_error(f"Virtual Machine State write failed: {name}")

	if unchanged:
		update_reported_states(unchanged, synced_at)


def update_reported_states(states: dict[str, frappe._dict], synced_at: datetime) -> None:
	"""Write the reported values of many rows in one statement."""
	table = frappe.qb.DocType("Virtual Machine State")
	query = frappe.qb.update(table).set(table.synced_at, synced_at).where(table.name.isin(list(states)))
	for field in REPORTED_FIELDS:
		value = Case()
		for name, state in states.items():
			value = value.when(table.name == name, state[field])
		query = query.set(table[field], value)
	query.run()
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


def get_reported_states(reported: object) -> dict[str, frappe._dict]:
	"""Return the reported values of each virtual machine in a Metal sync response."""
	if not isinstance(reported, dict):
		raise ValueError("Metal virtual machine response must be an object")

	states = {}
	for name, state in reported.items():
		if not isinstance(state, dict):
			raise ValueError("Metal virtual machine response has invalid values")
		status = state.get("status")
		sleep_state = state.get("sleep_state") or ""
		sleep_snapshot_id = state.get("sleep_snapshot_id") or None
		if (
			not isinstance(status, str)
			or not status
			or sleep_state not in ("", *SLEEP_STATES)
			or not isinstance(sleep_snapshot_id, str | None)
		):
			raise ValueError("Metal virtual machine response has invalid values")
		states[name] = frappe._dict(
			status=status,
			sleep_state=sleep_state,
			sleep_snapshot_id=sleep_snapshot_id,
			last_request_at=timestamp_field(state, "last_request_at"),
		)
	return states


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
