import frappe


def execute() -> None:
	"""Keep existing migrations on the disk-only default."""
	frappe.db.sql(
		"""UPDATE `tabVirtual Machine Migration`
		SET migrate_memory_snapshot = 0 WHERE migrate_memory_snapshot IS NULL"""
	)
