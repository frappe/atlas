"""Rename the `Site.pilot` Link field → `console`.

The field links a self-serve Site to its attached admin console (a
`Site(kind="pilot-console")` on the same backing VM). After the Site/Pilot merge
the "pilot" name is legacy — the field holds a console — so rename it. The DocType
JSON in this commit ships the new fieldname; this carries the stored column's data
across and drops the old one.

`Site` is a normal DocType (a real `tabSite` column). This runs **post_model_sync**,
so the JSON-driven schema sync has already added the `console` column (Frappe never
drops the orphaned `pilot`). We copy the data across and drop `pilot`. Done as raw
DDL rather than `frappe.model.rename_field`, which only copies values and assumes the
new field is already in the doctype meta — a subtlety that leaves the old column and
strands nothing here but is easy to get wrong.

Idempotent: no-ops once `pilot` is gone (a fresh site's table shipped `console` only).
"""

import frappe


def execute() -> None:
	if not frappe.db.has_column("Site", "pilot"):
		return  # already migrated, or a fresh site that shipped `console`
	# model sync has already added `console`; carry any stored link across, then drop `pilot`.
	frappe.db.sql(
		"""UPDATE `tabSite`
		SET console = pilot
		WHERE (console IS NULL OR console = '')
		  AND pilot IS NOT NULL AND pilot != ''"""
	)
	frappe.db.sql_ddl("ALTER TABLE `tabSite` DROP COLUMN pilot")
