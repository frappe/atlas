"""Rename `description` → `title` on `Virtual Machine`.

The DocType JSON in this commit ships the new field. Frappe's natural
migration step won't carry over the legacy column's values to a
differently-named new column — we do an in-place column rename via DDL
before the JSON-driven sync runs, so existing rows keep their labels.

Idempotent: re-running on an already-renamed table is a no-op.
"""

import frappe


def execute():
	has_description = frappe.db.has_column("Virtual Machine", "description")
	has_title = frappe.db.has_column("Virtual Machine", "title")
	if has_title and not has_description:
		return  # already renamed
	if has_description and not has_title:
		frappe.db.sql_ddl(
			"ALTER TABLE `tabVirtual Machine` CHANGE `description` `title` VARCHAR(140)"
		)
		return
	if has_description and has_title:
		# Both columns exist (e.g. an earlier failed migration left them
		# both around). Copy any value still in `description` over to
		# `title` if `title` is empty, then drop nothing — leaving the
		# orphan column behind is harmless and removing it is destructive.
		frappe.db.sql(
			"UPDATE `tabVirtual Machine` SET `title` = `description` "
			"WHERE (`title` IS NULL OR `title` = '') AND `description` IS NOT NULL"
		)
