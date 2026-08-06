"""Rename `Atlas Settings.satellite_public_keys` → `service_public_keys`.

The field held the OpenSSH public key(s) of what used to be called the Satellite
orchestrator; the seam is now neutral — an external image/orchestration service
(e.g. chef) — so the field is renamed to match. The DocType JSON in this commit
ships the new fieldname; this carries the stored value across.

`Atlas Settings` is a Single, so its value lives in the `tabSingles` row keyed by
(`doctype`, `field`). Guarded on the legacy field/data still existing: idempotent, a
no-op once already renamed (or on a fresh site that never held the old name)."""

import frappe


def execute() -> None:
	if not _singles_has_field("satellite_public_keys"):
		return  # nothing to carry over (fresh site, or already migrated)
	frappe.reload_doc("atlas", "doctype", "atlas_settings")
	from frappe.model.utils.rename_field import rename_field

	rename_field("Atlas Settings", "satellite_public_keys", "service_public_keys")


def _singles_has_field(field: str) -> bool:
	return bool(
		frappe.db.sql(
			"""SELECT 1 FROM `tabSingles`
			WHERE doctype = 'Atlas Settings' AND field = %s LIMIT 1""",
			field,
		)
	)
