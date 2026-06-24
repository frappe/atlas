"""Catalog-row seeding shared by the Settings `setup()` setters and bootstrap.

A `<Vendor> Settings.default_size` / `.default_image` is a `reqd` Link to a
`Provider Size` / `Provider Image` row. Before the Single can hold the slug the
row must exist, so the setters seed an empty-metadata placeholder here (the real
per-slug metadata is filled in later by `discover()` + `upsert_catalog`). Lifted
out of bootstrap.py so the explicit setters and the back-compat bootstrap path
write through one implementation.
"""

import json

import frappe


def ensure_provider_size(provider_type: str, slug: str) -> None:
	"""Create a placeholder `Provider Size` row for `provider_type/slug` if absent."""
	name = f"{provider_type}/{slug}"
	if frappe.db.exists("Provider Size", name):
		return
	frappe.get_doc(
		{
			"doctype": "Provider Size",
			"provider_type": provider_type,
			"slug": slug,
			"enabled": 1,
			"provider_metadata": json.dumps({}),
		}
	).insert(ignore_permissions=True)


def ensure_provider_image(provider_type: str, slug: str) -> None:
	"""Create a placeholder `Provider Image` row for `provider_type/slug` if absent."""
	name = f"{provider_type}/{slug}"
	if frappe.db.exists("Provider Image", name):
		return
	frappe.get_doc(
		{
			"doctype": "Provider Image",
			"provider_type": provider_type,
			"slug": slug,
			"enabled": 1,
			"provider_metadata": json.dumps({}),
		}
	).insert(ignore_permissions=True)
