"""Migrate `Server Provider` polymorphic blob → Provider abstraction.

After this patch, the data shape is:

- `Provider` (renamed from `Server Provider`) — thin link table:
  provider_name, provider_type, is_active.
- `Atlas Settings` (Single) — picks the active Provider and holds the
  SSH triplet (fingerprint, public key, private-key path).
- `DigitalOcean Settings` (Single) — api_token, region, default_size,
  default_image.
- `Self-Managed Settings` (Single) — stub.
- `Provider Size` / `Provider Image` — vendor catalogs, keyed
  `{provider_type}/{slug}`.

Runs in `pre_model_sync` so the legacy columns still exist when we read
them. Idempotent — re-running on an already-migrated site is a no-op.

The patch performs the migration in a specific order to avoid clashing
with Frappe's `bench migrate` JSON sync:
  1. Read legacy fields off `tabServer Provider` into in-memory state.
  2. `frappe.rename_doc` — `Server Provider` → `Provider`. Renames the
     table and updates every row's `doctype` field.
  3. Drop the legacy columns from `tabProvider`.
  4. `reload_doc` the new Singles + catalog DocTypes (their JSONs name
     them `Atlas Settings`, etc., so reload installs them fresh).
  5. Write the migrated state into the Singles.
  6. Migrate `Server.size` values to `{provider_type}/{slug}` and drop
     `Server.region`.
"""

from __future__ import annotations

import json

import frappe
import frappe.utils.password


def execute() -> None:
	legacy_state = _read_legacy_state()
	_rename_server_provider_doctype()
	_drop_legacy_provider_columns()
	_install_new_doctypes()
	_write_settings(legacy_state)
	_seed_catalogs()
	_migrate_server_table()
	_cleanup_legacy_onboarding()


def _read_legacy_state() -> dict | None:
	"""Read the most-recent active DigitalOcean row plus an SSH fallback.

	Returns None when the legacy doctype/columns are already gone — the
	patch has already run.
	"""
	if not frappe.db.exists("DocType", "Server Provider"):
		return None
	if not frappe.db.has_column("Server Provider", "api_token"):
		return None

	rows = frappe.db.sql(
		"""
		SELECT name, provider_type, is_active,
		       ssh_key_id, ssh_private_key_path,
		       default_region, default_size, default_image
		FROM `tabServer Provider`
		ORDER BY creation DESC
		""",
		as_dict=True,
	)
	if not rows:
		return None

	active_do = next(
		(r for r in rows if r["provider_type"] == "DigitalOcean" and r["is_active"]),
		None,
	)
	fallback = next((r for r in rows if r["is_active"]), None)

	state: dict = {"active_provider": None, "ssh": None, "digitalocean": None}

	if active_do:
		token = frappe.utils.password.get_decrypted_password(
			"Server Provider", active_do["name"], "api_token", raise_exception=False
		)
		state["active_provider"] = active_do["name"]
		state["ssh"] = {
			"ssh_fingerprint": active_do["ssh_key_id"],
			"ssh_private_key_path": active_do["ssh_private_key_path"],
		}
		state["digitalocean"] = {
			"api_token": token,
			"region": active_do["default_region"],
			"default_size_slug": active_do["default_size"],
			"default_image_slug": active_do["default_image"],
		}
	elif fallback:
		state["active_provider"] = fallback["name"]
		state["ssh"] = {
			"ssh_fingerprint": fallback["ssh_key_id"],
			"ssh_private_key_path": fallback["ssh_private_key_path"],
		}

	return state


def _rename_server_provider_doctype() -> None:
	if not frappe.db.exists("DocType", "Server Provider"):
		return
	frappe.rename_doc("DocType", "Server Provider", "Provider", force=True)
	frappe.clear_cache()


def _drop_legacy_provider_columns() -> None:
	for column in (
		"api_token",
		"ssh_key_id",
		"ssh_private_key_path",
		"default_region",
		"default_size",
		"default_image",
	):
		if frappe.db.has_column("Provider", column):
			frappe.db.sql_ddl(f"ALTER TABLE `tabProvider` DROP COLUMN `{column}`")


def _install_new_doctypes() -> None:
	"""Force-load the new DocType JSONs. Safe to call multiple times."""
	for doctype in (
		"provider",
		"atlas_settings",
		"digitalocean_settings",
		"self_managed_settings",
		"provider_size",
		"provider_image",
	):
		frappe.reload_doc("atlas", "doctype", doctype, force=True)


def _write_settings(state: dict | None) -> None:
	if state is None:
		return

	if state["ssh"]:
		for field, value in state["ssh"].items():
			if value:
				frappe.db.set_single_value(
					"Atlas Settings", field, value, update_modified=False
				)
	if state["active_provider"]:
		frappe.db.set_single_value(
			"Atlas Settings", "provider", state["active_provider"], update_modified=False
		)
	if state["digitalocean"]:
		do = state["digitalocean"]
		default_size = _ensure_provider_size("DigitalOcean", do["default_size_slug"])
		default_image = _ensure_provider_image("DigitalOcean", do["default_image_slug"])
		if do["region"]:
			frappe.db.set_single_value(
				"DigitalOcean Settings", "region", do["region"], update_modified=False
			)
		if default_size:
			frappe.db.set_single_value(
				"DigitalOcean Settings", "default_size", default_size, update_modified=False
			)
		if default_image:
			frappe.db.set_single_value(
				"DigitalOcean Settings", "default_image", default_image, update_modified=False
			)
		if do["api_token"]:
			frappe.utils.password.set_encrypted_password(
				"DigitalOcean Settings",
				"DigitalOcean Settings",
				do["api_token"],
				"api_token",
			)


def _seed_catalogs() -> None:
	from atlas.atlas.providers.digitalocean import (
		DIGITALOCEAN_MONTHLY_COST_USD,
		KNOWN_DIGITALOCEAN_IMAGES,
		KNOWN_DIGITALOCEAN_SIZES,
	)

	for slug in KNOWN_DIGITALOCEAN_SIZES:
		_ensure_provider_size(
			"DigitalOcean",
			slug,
			monthly_cost_usd=DIGITALOCEAN_MONTHLY_COST_USD.get(slug),
		)
	for slug in KNOWN_DIGITALOCEAN_IMAGES:
		_ensure_provider_image("DigitalOcean", slug)


def _migrate_server_table() -> None:
	if not frappe.db.table_exists("Server"):
		return

	for server in frappe.db.sql(
		"SELECT name, provider, size FROM `tabServer`", as_dict=True
	):
		size = server["size"]
		if not size or "/" in size:
			continue
		provider_type = frappe.db.get_value("Provider", server["provider"], "provider_type")
		if not provider_type:
			continue
		size_name = _ensure_provider_size(provider_type, size)
		frappe.db.set_value("Server", server["name"], "size", size_name, update_modified=False)

	if frappe.db.has_column("Server", "region"):
		frappe.db.sql_ddl("ALTER TABLE `tabServer` DROP COLUMN `region`")


def _cleanup_legacy_onboarding() -> None:
	"""Delete the old "Add Server Provider" Onboarding Step row.

	`bench migrate` installs the new "Configure Atlas" step from JSON; the
	old row stays orphaned because its DocType name doesn't match any JSON.
	No-op on sites where the row was never created.
	"""
	if frappe.db.exists("Onboarding Step", "Add Server Provider"):
		frappe.delete_doc(
			"Onboarding Step",
			"Add Server Provider",
			force=True,
			ignore_permissions=True,
		)


def _ensure_provider_size(provider_type: str, slug: str | None, monthly_cost_usd: int | None = None) -> str:
	if not slug:
		return ""
	name = f"{provider_type}/{slug}"
	if frappe.db.exists("Provider Size", name):
		if monthly_cost_usd is not None and not frappe.db.get_value(
			"Provider Size", name, "monthly_cost_usd"
		):
			frappe.db.set_value(
				"Provider Size", name, "monthly_cost_usd", monthly_cost_usd
			)
		return name
	frappe.get_doc({
		"doctype": "Provider Size",
		"provider_type": provider_type,
		"slug": slug,
		"enabled": 1,
		"monthly_cost_usd": monthly_cost_usd,
		"provider_metadata": json.dumps({}),
	}).insert(ignore_permissions=True)
	return name


def _ensure_provider_image(provider_type: str, slug: str | None) -> str:
	if not slug:
		return ""
	name = f"{provider_type}/{slug}"
	if frappe.db.exists("Provider Image", name):
		return name
	frappe.get_doc({
		"doctype": "Provider Image",
		"provider_type": provider_type,
		"slug": slug,
		"enabled": 1,
		"provider_metadata": json.dumps({}),
	}).insert(ignore_permissions=True)
	return name
