import frappe


def execute() -> None:
	"""Store the pause flag row, so create can lock it."""
	frappe.db.set_single_value("Atlas Settings", "is_vm_migration_paused", 0)
