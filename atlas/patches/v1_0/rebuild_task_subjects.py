"""Rebuild every Task row's `subject` per the verb/verb-noun rule.

Replaces the legacy `<verb> · <target>` shape with the simpler verb (or
verb-noun) label. Idempotent — applies the same `SCRIPT_LABELS` lookup
the controller's `_build_subject` uses now, so re-running is a no-op.
"""

import frappe

from atlas.atlas.doctype.task.task import SCRIPT_LABELS


def execute() -> None:
	rows = frappe.db.sql(
		"SELECT name, script FROM `tabTask`",
		as_dict=True,
	)
	for row in rows:
		subject = SCRIPT_LABELS.get(row.script, row.script or "Task")
		frappe.db.set_value(
			"Task", row.name, "subject", subject, update_modified=False,
		)
