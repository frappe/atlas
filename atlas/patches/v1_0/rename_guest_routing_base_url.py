"""Rename `Atlas Settings.satellite_routing_base_url` → `guest_routing_base_url`.

The field is really the guest routing base URL — the external service that serves
this Atlas's guest routing API (injected into each guest as ROUTING_BASE_URL); the
old "satellite" framing predates the neutral seam. The DocType JSON in this commit
ships the new fieldname; this carries the stored value across. (The guest VARIABLE
`ROUTING_BASE_URL` is the Boat guest contract and is deliberately NOT renamed.)

`Atlas Settings` is a Single, so its value lives in the `tabSingles` row keyed by
(`doctype`, `field`). Guarded on the legacy field/data still existing: idempotent, a
no-op once already renamed (or on a fresh site that never held the old name)."""

import frappe


def execute() -> None:
	if not _singles_has_field("satellite_routing_base_url"):
		return  # nothing to carry over (fresh site, or already migrated)
	frappe.reload_doc("atlas", "doctype", "atlas_settings")
	from frappe.model.utils.rename_field import rename_field

	rename_field("Atlas Settings", "satellite_routing_base_url", "guest_routing_base_url")


def _singles_has_field(field: str) -> bool:
	return bool(
		frappe.db.sql(
			"""SELECT 1 FROM `tabSingles`
			WHERE doctype = 'Atlas Settings' AND field = %s LIMIT 1""",
			field,
		)
	)
