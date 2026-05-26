import frappe


def execute():
	frappe.db.sql(
		"UPDATE `tabVirtual Machine` SET status='Terminated' WHERE status='Archived'"
	)
