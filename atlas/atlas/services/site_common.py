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


def terminate_backing_vm(doc) -> None:
	"""Terminate the aggregate's backing VM if one was created and is not already gone.

	No-op when the aggregate is ATTACHED: it shares a VM another aggregate created and
	tears down, so terminating it here would double-terminate (and race the owner's own
	VM teardown). An attached aggregate's teardown is only its Subdomain + its own row.
	(A bench-site is never attached, so the guard is inert for it.)

	`front_door_terminating` tells the VM this aggregate is already tearing itself down,
	so it skips `_terminate_front_doors` — the aggregate marks and saves itself in
	`terminate()`, and re-marking there would emit its status event twice."""
	if doc.attached:
		return
	if not doc.virtual_machine or not frappe.db.exists("Virtual Machine", doc.virtual_machine):
		return
	vm = frappe.get_doc("Virtual Machine", doc.virtual_machine)
	if vm.status != "Terminated":
		vm.flags.front_door_terminating = True
		vm.terminate()
