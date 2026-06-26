"""Drop the denormalized `region` field from Site, Subdomain, Port Mapping, and
Virtual Machine, and rename existing Port Mapping rows off the `{region}-{port}`
autoname.

Atlas is single-region (`Atlas Settings.region`, `placement.atlas_region`, the
one source of truth — each instance runs exactly one region, Central picks the
instance). The per-row `region` on these four doctypes was always a copy of that
single value, never used to make a decision that could differ from it, so it is
gone. The join/filter that read it (`map_for_region`, `port_map_for_region`,
`_proxy_vms_in_region`) now query the whole single-region fleet directly.

Two cleanups, both idempotent:

1. **Rename Port Mapping rows.** The autoname moved from `{region}-{public_port}`
   to `{protocol}-{public_port}` (the region prefix is meaningless with one
   region; protocol keeps the name readable and `public_port` is now globally
   unique). Rename each existing row to its new name before the column drop, so
   no row keeps a stale `<region>-<port>` name.

2. **Drop the orphan `region` columns.** Frappe leaves a removed Data field's
   column in place on migrate; drop the four explicitly so no stale column
   lingers. `Root Domain.region` is deliberately KEPT (it freezes the region at
   insert so a later Settings change can't re-point an existing domain) and is
   not touched here.
"""

import frappe

# DocTypes whose `region` column is dropped. Root Domain is NOT here.
_DOCTYPES = (
	"Site",
	"Subdomain",
	"Port Mapping",
	"Virtual Machine",
)


def execute() -> None:
	_rename_port_mappings()
	_drop_region_columns()


def _rename_port_mappings() -> None:
	"""Rename `{region}-{port}` → `{protocol}-{port}` for every existing row.

	Reads straight from the table (the `region` column still exists at this point,
	pre-drop). A row already on the new name (a half-run, or a row inserted after
	the code change) has no `region`-prefixed name to fix and is skipped by the
	`name != new_name` guard inside rename_doc."""
	if not frappe.db.table_exists("Port Mapping"):
		return  # fresh DB — table not created yet, nothing to rename
	if not frappe.db.has_column("Port Mapping", "region"):
		return  # columns already dropped — nothing to rename
	rows = frappe.db.sql("""SELECT name, protocol, public_port FROM `tabPort Mapping`""", as_dict=True)
	for row in rows:
		new_name = f"{row['protocol']}-{row['public_port']}"
		if row["name"] == new_name:
			continue
		# force: the new name is guaranteed unique (public_port is unique), so a
		# collision here would be a real data bug worth surfacing, not swallowing.
		frappe.rename_doc("Port Mapping", row["name"], new_name, force=True, show_alert=False)


def _drop_region_columns() -> None:
	for doctype in _DOCTYPES:
		if frappe.db.table_exists(doctype) and frappe.db.has_column(doctype, "region"):
			frappe.db.sql_ddl(f"ALTER TABLE `tab{doctype}` DROP COLUMN `region`")
