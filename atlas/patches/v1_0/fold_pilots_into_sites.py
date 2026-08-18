"""Fold every `tabPilot` row into `tabSite` as a `kind="pilot-console"` Site.

The Site and Pilot DocTypes merged: a bench console is now a `Site(kind="pilot-console")`
(services/site_console.py). New consoles are already created as Sites (create_vm,
_provision_pilot); this migrates the rows that predate the flip so the Pilot DocType can be
deleted without losing them. Runs before `delete_pilot_doctype` (patches.txt order).

Each Pilot becomes a Site of the SAME name (both autoname to `<subdomain>.<region>`), so a
bench-site Site's `pilot` link — which already holds the console's FQDN — keeps resolving
after the retarget to "Site" with no fixup. The backing VM + Subdomain rows are shared and
unchanged. We `db_insert` (not `insert`) so the console's after_insert does NOT run — the
VM already exists and must not be re-provisioned. Idempotent: skips a name already folded.
"""

import frappe

# Pilot fields carried onto the merged Site row (the union that has meaning for a console).
_CARRY = (
	"subdomain",
	"tenant",
	"virtual_machine",
	"subdomain_doc",
	"status",
	"login_url",
	"login_url_expires_at",
	"build_mode",
	"attached",
)


def execute():
	if not frappe.db.table_exists("Pilot"):
		return
	for pilot in frappe.get_all("Pilot", fields=["*"]):
		if frappe.db.exists("Site", pilot.name):
			continue  # already folded (idempotent re-run)
		site = frappe.new_doc("Site")
		site.update({field: pilot.get(field) for field in _CARRY})
		site.kind = "pilot-console"
		# Preserve the row's identity + audit trail: same name (FQDN), same timestamps/owner.
		site.name = pilot.name
		site.flags.name_set = True
		site.creation = pilot.creation
		site.modified = pilot.modified
		site.owner = pilot.owner
		site.modified_by = pilot.modified_by
		# db_insert skips before/after_insert + validate — a raw row copy, so folding an
		# already-provisioned console does not re-run provisioning or re-emit events.
		site.db_insert()
