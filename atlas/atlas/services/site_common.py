"""Teardown leaves shared by the two front-door aggregates (Site and Pilot).

Both DocTypes are the same aggregate shape — one routing identity, a backing
Virtual Machine, and a `Subdomain` proxy-map row — so their `terminate()` cleanup
is identical. These free functions take the aggregate doc so the one copy serves
both controllers (and, after the Site+Pilot merge, both per-kind flows). See
spec/14-self-serve.md.
"""

import frappe


def delete_subdomain(doc) -> None:
	"""Drop the aggregate's proxy-map row (its on_trash reconciles the fleet). No-op
	when it never began serving (no Subdomain was created).

	Clear `subdomain_doc` first: while the doc's Link field still references the
	Subdomain, Frappe's link-integrity guard refuses the delete (LinkExistsError).
	The guard queries the DB, so the null must be persisted (db_set), not just set
	in-memory, before the delete. Same clear-then-remove order terminate() uses for
	the VM."""
	subdomain = doc.subdomain_doc
	if not subdomain:
		return
	doc.db_set("subdomain_doc", None)
	if frappe.db.exists("Subdomain", subdomain):
		frappe.delete_doc("Subdomain", subdomain, ignore_permissions=True)
