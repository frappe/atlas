"""Make `Atlas Settings.region` the single source of truth for this Atlas's region.

The region used to live in three places: `Central Settings.region`, the
`atlas_tls_region` / `atlas_do_region` site config, and (denormalized per row)
`Root Domain.region`. This collapses it onto one Single field, `Atlas Settings.region`.

Two steps, both against `tabSingles` (a Single's values are rows keyed by
(`doctype`, `field`)):

1. **Backfill `Atlas Settings.region`** from the best existing source, in
   precedence order: a value already there (a half-run) wins; else the legacy
   `Central Settings.region`; else the active `Root Domain.region` (every Root
   Domain carried the region, and Atlas is single-region so the active one is
   authoritative); else the site config (`atlas_tls_region` → `atlas_do_region`).
   Leaves the field blank only when nothing is configured — the operator sets it
   on the form (it is `reqd`), and the fail-loud `placement.atlas_region` reader
   makes a blank obvious rather than silently misrouting.

2. **Drop the dead `Central Settings.region` row.** Its value (if any) was carried
   to Atlas Settings in step 1; the field is gone from the DocType.

Idempotent: step 1 no-ops once Atlas Settings already has a region; step 2 no-ops
once the Central row is gone.
"""

import frappe


def execute() -> None:
	_backfill_atlas_settings_region()
	_drop_central_settings_region()


def _backfill_atlas_settings_region() -> None:
	if _single_value("Atlas Settings", "region"):
		return  # already set (a half-run, or a fresh install seeded by bootstrap)

	region = (
		_single_value("Central Settings", "region")
		or _active_root_domain_region()
		or frappe.conf.get("atlas_tls_region")
		or frappe.conf.get("atlas_do_region")
	)
	if not region:
		return  # nothing to backfill from — operator sets it on the form (reqd)

	frappe.db.set_single_value("Atlas Settings", "region", region, update_modified=False)


def _drop_central_settings_region() -> None:
	frappe.db.delete("Singles", {"doctype": "Central Settings", "field": "region"})


def _single_value(doctype: str, field: str) -> str | None:
	"""Read a Single value straight off `tabSingles`, so this works even after the
	field is removed from the DocType (Central Settings.region) and before the
	field is loaded onto the meta (a fresh Atlas Settings.region)."""
	# order_by=None: `tabSingles` has no `creation` column, and get_value's default
	# `ORDER BY creation` would raise "Unknown column 'creation'".
	value = frappe.db.get_value("Singles", {"doctype": doctype, "field": field}, "value", order_by=None)
	return value or None


def _active_root_domain_region() -> str | None:
	rows = frappe.get_all(
		"Root Domain",
		filters={"is_active": 1},
		pluck="region",
		order_by="creation asc",
		limit=1,
	)
	return rows[0] if rows else None
