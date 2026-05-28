"""Rename `description` → `title` on `Virtual Machine Image`.

Mirror of the Phase 4 VM patch; same idempotency rules. Idempotent
re-runs are safe.
"""

import frappe


def execute():
	has_description = frappe.db.has_column("Virtual Machine Image", "description")
	has_title = frappe.db.has_column("Virtual Machine Image", "title")
	if has_title and not has_description:
		return
	if has_description and not has_title:
		frappe.db.sql_ddl(
			"ALTER TABLE `tabVirtual Machine Image` CHANGE `description` `title` VARCHAR(140)"
		)
		return
	if has_description and has_title:
		frappe.db.sql(
			"UPDATE `tabVirtual Machine Image` SET `title` = `description` "
			"WHERE (`title` IS NULL OR `title` = '') AND `description` IS NOT NULL"
		)
