"""Catalog-row seeding + default resolution, shared by the Settings `setup()`
setters and bootstrap.

The Provision Server dialog prefills from the `Provider Size` / `Provider Image`
row marked `is_default` (at most one per provider_type). `ensure_provider_*` seeds
an empty-metadata placeholder row (the real per-slug metadata arrives later via
`discover()` + `upsert_catalog`); `set_default` marks one row default (clearing any
sibling through the row controller); `default_name` resolves the marked row's name
for the dialog and `provision_server`. Lifted out of bootstrap.py so the explicit
setters and the back-compat bootstrap path write through one implementation.
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


def clear_other_defaults(doctype: str, provider_type: str, keep: str) -> None:
	"""Unset `is_default` on every `doctype` row of `provider_type` except `keep`.

	Enforces the one-default-per-provider invariant: a `Provider Size` / `Provider
	Image` controller calls this from `validate()` when its own `is_default` is set,
	so setting a new default flips the previous one off in the same save. Writes with
	`set_value` (no recursive save) and skips `keep`'s own row."""
	others = frappe.get_all(
		doctype,
		filters={"provider_type": provider_type, "is_default": 1, "name": ("!=", keep)},
		pluck="name",
	)
	for name in others:
		frappe.db.set_value(doctype, name, "is_default", 0, update_modified=False)


def set_default(doctype: str, provider_type: str, slug: str) -> None:
	"""Mark `provider_type/slug` the lone default for `doctype` (Provider Size/Image).

	Also (re-)enables the row: the default must be selectable, and a configured
	default slug outside the provider's `discover()` set would otherwise have been
	disabled by `upsert_catalog`'s prune pass just before this runs — leaving a
	disabled default that `default_name` (which filters `enabled=1`) can't resolve.

	Saves the row through the ORM so its `validate()` runs `clear_other_defaults`;
	the named row must already exist (the setters seed/verify it first)."""
	doc = frappe.get_doc(doctype, f"{provider_type}/{slug}")
	doc.is_default = 1
	doc.enabled = 1
	doc.save(ignore_permissions=True)


def default_name(doctype: str, provider_type: str) -> str:
	"""The default row's `name` (e.g. "DigitalOcean/s-2vcpu-4gb") for `provider_type`,
	or "" if none is marked. This is the prefixed identifier the Provision Server
	modal Link and `ProvisionRequest` want — not the bare vendor slug."""
	name = frappe.db.get_value(
		doctype, {"provider_type": provider_type, "is_default": 1, "enabled": 1}, "name"
	)
	return name or ""
