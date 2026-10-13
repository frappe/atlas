import frappe


def execute() -> None:
	"""Label existing migration transfer rows as disk snapshots."""
	frappe.db.sql(
		"""UPDATE `tabVirtual Machine Migration Transfer`
		SET snapshot_type = 'Disk' WHERE snapshot_type IS NULL OR snapshot_type = ''"""
	)
