"""Delete the VPN Tunnel DocType — the host-terminated WireGuard broker is retired.

The customer gateway VM on the mesh (`VPN Peer`, `services/customer_gateway.py`;
spec/25, spec/26) superseded the host-terminated broker. Remove the DocType record,
then DROP `tabVPN Tunnel` explicitly — a force delete removes the metadata but leaves
the table behind, which would strand it as an orphan. Guarded so a fresh install (or
a re-run) is a clean no-op.
"""

import frappe


def execute():
	if frappe.db.exists("DocType", "VPN Tunnel"):
		frappe.delete_doc("DocType", "VPN Tunnel", force=True, ignore_permissions=True)
	frappe.db.sql_ddl("DROP TABLE IF EXISTS `tabVPN Tunnel`")
