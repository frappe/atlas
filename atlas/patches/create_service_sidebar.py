import frappe

SERVICE_DOCTYPES = [
	"Proxy Server",
	"Cargo Server",
	"IPv6 Router Server",
	"WireGuard Gateway Server",
]


def create_service_sidebar():
	"""Give the Service module an explicit sidebar.

	Without a Workspace Sidebar record, Frappe auto-generates one from the
	module's doctypes and keeps only the first three, so the WireGuard Gateway
	Server never showed up in the sidebar.
	"""
	if frappe.db.exists("Workspace Sidebar", {"name": "Service", "for_user": None}):
		return

	sidebar = frappe.new_doc("Workspace Sidebar")
	sidebar.title = "Service"
	sidebar.module = "Service"
	sidebar.header_icon = "server"

	for doctype in SERVICE_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue
		sidebar.append(
			"items",
			{"type": "Link", "link_type": "DocType", "link_to": doctype, "label": doctype},
		)

	sidebar.insert(ignore_permissions=True)
