import frappe


def execute() -> None:
	"""Keep hostname changes disabled for existing servers."""
	server = frappe.qb.DocType("Metal Server")
	frappe.qb.update(server).set(server.set_hostname, 0).where(server.set_hostname.isnull()).run()
