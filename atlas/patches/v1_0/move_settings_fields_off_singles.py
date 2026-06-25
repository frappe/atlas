"""Tidy the Settings Singles: relocate two fields, collapse three telemetry
fields into one.

All moves are against `tabSingles` (a Single's values live in rows keyed by
(`doctype`, `field`), so we rewrite rows, not columns):

1. **`Atlas Settings.ssh_key_id` → the active vendor's Settings.** The field
   holds the *vendor's* handle for the uploaded key (DO key id / fingerprint,
   Scaleway IAM id) — meaningless to any other vendor, so it never belonged on
   the vendor-agnostic Single. Carry the stored value to the Single matching the
   current `Atlas Settings.provider_type`; for Self-Managed / Fake / unset there
   is no vendor field and the handle was inert, so just drop it. `ssh_public_key`
   and `ssh_private_key_path` are genuinely one-per-Atlas and stay put.

2. **`Route53 Settings.domain_provider_type` → `Atlas Settings.dns_provider_type`.**
   The active DNS vendor is an Atlas-instance switch (a sibling of `provider_type`
   / `tls_provider_type`), not a Route 53 credential — it sat on Route53 Settings
   only by accident. It is also renamed off the "Domain Provider" misnomer. The
   `Root Domain` denormalized copy is renamed by a separate pre_model_sync patch
   (`rename_root_domain_dns_provider_type`).

3. **Central Settings telemetry → one `status` breadcrumb.** `registered_on`
   and `last_sync` were written but never read — drop them. `last_event_status`
   carried the only value worth keeping (last register / event-delivery
   outcome); rename it to `status`.

Idempotent: each step no-ops once its source rows are gone.
"""

import frappe

_VENDOR_SINGLE = {
	"DigitalOcean": "DigitalOcean Settings",
	"Scaleway": "Scaleway Settings",
}


def execute() -> None:
	_move_ssh_key_id_to_vendor_single()
	_move_dns_provider_type_to_atlas_settings()
	_collapse_central_telemetry()


def _move_dns_provider_type_to_atlas_settings() -> None:
	value = frappe.db.get_value(
		"Singles",
		{"doctype": "Route53 Settings", "field": "domain_provider_type"},
		"value",
		order_by=None,
	)
	if value is None:
		return  # already migrated, or never set
	if frappe.db.exists("Singles", {"doctype": "Atlas Settings", "field": "dns_provider_type"}):
		# Target already populated (half-run): keep it, drop the stale source row.
		frappe.db.delete("Singles", {"doctype": "Route53 Settings", "field": "domain_provider_type"})
		return
	frappe.db.sql(
		"""UPDATE `tabSingles`
		SET doctype = 'Atlas Settings', field = 'dns_provider_type'
		WHERE doctype = 'Route53 Settings' AND field = 'domain_provider_type'"""
	)


def _move_ssh_key_id_to_vendor_single() -> None:
	value = frappe.db.get_value(
		"Singles", {"doctype": "Atlas Settings", "field": "ssh_key_id"}, "value", order_by=None
	)
	if value is None:
		return  # already migrated, or never set

	provider_type = frappe.db.get_value(
		"Singles", {"doctype": "Atlas Settings", "field": "provider_type"}, "value", order_by=None
	)
	target = _VENDOR_SINGLE.get(provider_type)
	if target and value:
		# Repoint the row at the vendor Single (overwriting any value already
		# carried there by a half-run, which can only be this same handle).
		frappe.db.delete("Singles", {"doctype": target, "field": "ssh_key_id"})
		frappe.db.sql(
			"""UPDATE `tabSingles`
			SET doctype = %s
			WHERE doctype = 'Atlas Settings' AND field = 'ssh_key_id'""",
			target,
		)
	else:
		# No vendor field to hold it (Self-Managed / Fake / unset): the handle was
		# inert, drop it.
		frappe.db.delete("Singles", {"doctype": "Atlas Settings", "field": "ssh_key_id"})


def _collapse_central_telemetry() -> None:
	for dead in ("registered_on", "last_sync"):
		frappe.db.delete("Singles", {"doctype": "Central Settings", "field": dead})

	has_old = frappe.db.exists("Singles", {"doctype": "Central Settings", "field": "last_event_status"})
	if not has_old:
		return
	if frappe.db.exists("Singles", {"doctype": "Central Settings", "field": "status"}):
		# Both exist (half-run): prefer the new value, fall back to the legacy
		# one when the new is empty, then drop the stale name.
		frappe.db.sql(
			"""UPDATE `tabSingles` AS new_row
			JOIN `tabSingles` AS old_row
			  ON old_row.doctype = 'Central Settings' AND old_row.field = 'last_event_status'
			SET new_row.value = old_row.value
			WHERE new_row.doctype = 'Central Settings' AND new_row.field = 'status'
			  AND (new_row.value IS NULL OR new_row.value = '')
			  AND old_row.value IS NOT NULL AND old_row.value != ''"""
		)
		frappe.db.delete("Singles", {"doctype": "Central Settings", "field": "last_event_status"})
		return
	frappe.db.sql(
		"""UPDATE `tabSingles`
		SET field = 'status'
		WHERE doctype = 'Central Settings' AND field = 'last_event_status'"""
	)
