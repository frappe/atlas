"""Relocate the default size/image from the vendor Settings Singles onto the
catalog rows themselves.

`DigitalOcean Settings.default_size` / `.default_image` (and the Scaleway pair)
were `reqd` Link fields whose only job was to prefill the Provision Server modal.
The default now lives as `Provider Size.is_default` / `Provider Image.is_default`
(at most one per provider_type), so the provider's `discover()` can hint it and
the operator can flip it on the list anytime.

This runs post_model_sync: the `is_default` column already exists, and the old
field definitions are gone — but a Single's values live as rows in `tabSingles`
keyed by (`doctype`, `field`), which survive the field removal. We read those
orphaned rows, mark the referenced catalog row default (clearing any sibling),
then delete the stale rows. Idempotent: no-ops once the source rows are gone.
"""

import frappe

# (vendor Single, provider_type) — the Self-Managed/Fake providers had no Single.
_SINGLES = (
	("DigitalOcean Settings", "DigitalOcean"),
	("Scaleway Settings", "Scaleway"),
)


def execute() -> None:
	for single, provider_type in _SINGLES:
		_move_default("Provider Size", single, "default_size", provider_type)
		_move_default("Provider Image", single, "default_image", provider_type)


def _move_default(doctype: str, single: str, field: str, provider_type: str) -> None:
	# The stored value is the prefixed row name, e.g. "DigitalOcean/s-2vcpu-4gb".
	name = frappe.db.get_value("Singles", {"doctype": single, "field": field}, "value", order_by=None)
	if not name:
		return  # already migrated, or never set
	if frappe.db.exists(doctype, name):
		# Clear any sibling first so the one-per-provider invariant holds, then mark
		# this row default. Plain column writes — no doc save needed here.
		for sibling in frappe.get_all(
			doctype,
			filters={"provider_type": provider_type, "is_default": 1},
			pluck="name",
		):
			if sibling != name:
				frappe.db.set_value(doctype, sibling, "is_default", 0, update_modified=False)
		frappe.db.set_value(doctype, name, "is_default", 1, update_modified=False)
	frappe.db.delete("Singles", {"doctype": single, "field": field})
