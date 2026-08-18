"""Delete the Pilot DocType — its rows were folded into Site(kind="pilot-console").

Runs AFTER `fold_pilots_into_sites` (patches.txt order), so every Pilot has already
become a Site of the same name. Remove the DocType record, then DROP `tabPilot`
explicitly — a force delete removes the metadata but leaves the table behind, which
would strand it as an orphan. Guarded so a fresh install (where Pilot never existed)
is a clean no-op.
"""

import frappe


def execute():
	if frappe.db.exists("DocType", "Pilot"):
		frappe.delete_doc("DocType", "Pilot", force=True, ignore_permissions=True)
	frappe.db.sql_ddl("DROP TABLE IF EXISTS `tabPilot`")
