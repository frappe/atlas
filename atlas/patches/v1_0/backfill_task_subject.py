"""Backfill Task.subject for rows that pre-date the column being added.

The Task controller computes `subject` in `before_insert` for new rows, but
existing rows have NULL subjects and would show up in the desk's "Recent
activity" list as the random hash. We rebuild the subject from
(script, virtual_machine, server) with the same labelling rules the
controller uses.
"""

import frappe

from atlas.atlas.doctype.task.task import Task


def execute() -> None:
	names = frappe.db.get_all(
		"Task",
		filters={"subject": ("in", ("", None))},
		pluck="name",
	)
	for name in names:
		task = frappe.get_doc("Task", name)
		task.subject = task._build_subject()
		task.db_set("subject", task.subject, update_modified=False)
