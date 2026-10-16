import frappe


def execute() -> None:
	"""Keep existing hosts on their CPU template."""
	frappe.db.sql(
		"""UPDATE `tabMetal Server`
		SET is_cpu_template_disabled = 0 WHERE is_cpu_template_disabled IS NULL"""
	)
