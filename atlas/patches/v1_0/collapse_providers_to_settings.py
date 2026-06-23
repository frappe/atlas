"""Collapse the three Provider link-table DocTypes into Settings config.

After this patch the data shape is:

- `Atlas Settings` (Single) — `provider_type` (the active compute vendor, copied
  from the Provider row that `Atlas Settings.provider` named) + `tls_provider_type`
  (copied from the active `TLS Provider` row) + `fail_scripts` (copied from the
  Fake Provider row, dev-only fault injection).
- `Route53 Settings` (Single) — `domain_provider_type` (copied from the active
  `Domain Provider` row).
- `Server` — `provider_type` (copied from each Server's old `Provider` FK's type).
- `Root Domain` — `domain_provider_type` / `tls_provider_type` (copied from each
  row's old `Domain Provider` / `TLS Provider` FKs).
- `TLS Certificate` — `tls_provider_type` (copied from its old `TLS Provider` FK).
- The `Provider`, `Domain Provider`, `TLS Provider` DocTypes are dropped.

Runs in `pre_model_sync` so the legacy FK columns + the dead DocType tables still
exist when we read them. Idempotent — once the legacy tables are gone (a re-run, or
a fresh dev site re-bootstrapped under the new shape) it is a no-op.

Order:
  1. Read the active provider types + each row's denormalized FK into memory.
  2. `reload_doc` the touched DocTypes so the new `*_type` columns exist alongside
     the legacy FK columns (the JSONs already define the new fields).
  3. Write the migrated types onto the Singles + rows.
  4. Drop the legacy FK columns.
  5. Delete the three dead DocTypes + their tables.
"""

from __future__ import annotations

import frappe


def execute() -> None:
	# Nothing to do once the dead DocTypes are gone (a clean re-run, or a fresh site
	# already on the new shape). Keyed off the DocTypes — NOT the legacy columns,
	# because a partial run can drop the columns while the DocTypes survive, and that
	# half-state must still finish (delete the DocTypes), not be skipped.
	if not _any_dead_doctype_exists():
		return

	# Only read + copy the legacy values while the legacy columns are still present.
	# A partial re-run (columns already dropped, DocTypes not yet deleted) falls
	# straight through to the cleanup below — the type fields were written last run.
	if _legacy_columns_present():
		state = _read_legacy_state()
		_reload_touched_doctypes()
		_write_settings_types(state)
		_write_server_types(state)
		_write_root_domain_types(state)
		_write_tls_certificate_types(state)
		_drop_legacy_columns()

	_delete_dead_doctypes()


def _any_dead_doctype_exists() -> bool:
	return any(frappe.db.exists("DocType", dt) for dt in ("Provider", "Domain Provider", "TLS Provider"))


def _legacy_columns_present() -> bool:
	"""True while the legacy FK columns we copy from still exist. The compute path is
	the load-bearing one (`Server.provider`); the DNS/TLS columns may legitimately be
	absent on a site that never had the TLS layer."""
	return frappe.db.exists("DocType", "Provider") and frappe.db.has_column("Server", "provider")


def _provider_type_of(doctype: str, name: str | None) -> str | None:
	if not name or not frappe.db.exists(doctype, name):
		return None
	return frappe.db.get_value(doctype, name, "provider_type")


def _read_legacy_state() -> dict:
	"""Read the active types + every row's FK while the legacy columns still exist.

	Fails loud on data-integrity violations the happy path can't represent (an
	orphaned Server FK, more than one active vendor row, an active-provider pointer
	that disagrees with `is_active`) rather than silently corrupting the new shape —
	an operator must fix the data, then re-run."""
	active_provider = frappe.db.get_single_value("Atlas Settings", "provider")
	_assert_active_provider_consistent(active_provider)
	fail_scripts = None
	if active_provider and frappe.db.exists("Provider", active_provider):
		fail_scripts = frappe.db.get_value("Provider", active_provider, "fail_scripts")

	# Active DNS / TLS vendor = the single non-archived row of each (if any).
	active_domain_provider = _only_active("Domain Provider")
	active_tls_provider = _only_active("TLS Provider")

	servers = {
		row.name: _provider_type_of("Provider", row.provider)
		for row in frappe.get_all("Server", fields=["name", "provider"])
	}
	orphaned = sorted(name for name, ptype in servers.items() if not ptype)
	if orphaned:
		frappe.throw(
			"Cannot migrate: these Server(s) reference a missing/blank Provider, so their "
			f"provider_type can't be resolved — fix the data, then re-run: {', '.join(orphaned)}"
		)
	root_domains = {}
	if frappe.db.exists("DocType", "Root Domain") and frappe.db.has_column("Root Domain", "domain_provider"):
		for row in frappe.get_all("Root Domain", fields=["name", "domain_provider", "tls_provider"]):
			root_domains[row.name] = {
				"domain_provider_type": _provider_type_of("Domain Provider", row.domain_provider),
				"tls_provider_type": _provider_type_of("TLS Provider", row.tls_provider),
			}
	certs = {}
	if frappe.db.exists("DocType", "TLS Certificate") and frappe.db.has_column(
		"TLS Certificate", "tls_provider"
	):
		for row in frappe.get_all("TLS Certificate", fields=["name", "tls_provider"]):
			certs[row.name] = _provider_type_of("TLS Provider", row.tls_provider)

	return {
		"compute_type": _provider_type_of("Provider", active_provider),
		"fail_scripts": fail_scripts,
		"domain_type": active_domain_provider,
		"tls_type": active_tls_provider,
		"servers": servers,
		"root_domains": root_domains,
		"certs": certs,
	}


def _only_active(doctype: str) -> str | None:
	"""The provider_type of THE single non-archived row of a link-table doctype, or
	None when the doctype has none. Throws if more than one is active — the collapse
	keeps exactly one vendor, so an ambiguous fleet must be reconciled by hand first
	(otherwise the deletes below would silently drop the runners-up)."""
	if not frappe.db.exists("DocType", doctype):
		return None
	rows = frappe.get_all(doctype, filters={"is_active": 1}, fields=["name", "provider_type"])
	if len(rows) > 1:
		frappe.throw(
			f"Cannot migrate: {len(rows)} active {doctype} rows ({', '.join(r.name for r in rows)}); "
			"archive all but one, then re-run."
		)
	return rows[0].provider_type if rows else None


def _assert_active_provider_consistent(active_provider: str | None) -> None:
	"""`Atlas Settings.provider` must name THE active Provider — the source the
	compute migration trusts. If it names an archived/missing row while a different
	row is active, the migration would copy the wrong vendor's type + fail_scripts and
	then delete the real one. Fail loud."""
	active_rows = frappe.get_all("Provider", filters={"is_active": 1}, pluck="name")
	if len(active_rows) > 1:
		frappe.throw(
			f"Cannot migrate: {len(active_rows)} active Provider rows ({', '.join(active_rows)}); "
			"archive all but one, then re-run."
		)
	if not active_rows:
		return  # no active provider (fresh/abandoned site) — nothing to assert
	if active_provider != active_rows[0]:
		frappe.throw(
			f"Cannot migrate: Atlas Settings.provider ({active_provider!r}) is not the active "
			f"Provider ({active_rows[0]!r}). Point Atlas Settings at the active row, then re-run."
		)


def _reload_touched_doctypes() -> None:
	"""Load the new DocType JSONs so the `*_type` columns exist alongside the legacy
	FK columns we are about to copy from. Safe to call multiple times."""
	for doctype in (
		"atlas_settings",
		"route53_settings",
		"server",
		"root_domain",
		"tls_certificate",
	):
		frappe.reload_doc("atlas", "doctype", doctype, force=True)


def _write_settings_types(state: dict) -> None:
	if state["compute_type"]:
		frappe.db.set_single_value(
			"Atlas Settings", "provider_type", state["compute_type"], update_modified=False
		)
	if state["fail_scripts"]:
		frappe.db.set_single_value(
			"Atlas Settings", "fail_scripts", state["fail_scripts"], update_modified=False
		)
	if state["tls_type"]:
		frappe.db.set_single_value(
			"Atlas Settings", "tls_provider_type", state["tls_type"], update_modified=False
		)
	if state["domain_type"]:
		frappe.db.set_single_value(
			"Route53 Settings", "domain_provider_type", state["domain_type"], update_modified=False
		)


def _write_server_types(state: dict) -> None:
	for name, provider_type in state["servers"].items():
		if provider_type:
			frappe.db.set_value("Server", name, "provider_type", provider_type, update_modified=False)


def _write_root_domain_types(state: dict) -> None:
	for name, types in state["root_domains"].items():
		values = {k: v for k, v in types.items() if v}
		if values:
			frappe.db.set_value("Root Domain", name, values, update_modified=False)


def _write_tls_certificate_types(state: dict) -> None:
	for name, provider_type in state["certs"].items():
		if provider_type:
			frappe.db.set_value(
				"TLS Certificate", name, "tls_provider_type", provider_type, update_modified=False
			)


def _drop_legacy_columns() -> None:
	# Real (non-Single) tables: drop the dead FK columns.
	for doctype, column in (
		("Server", "provider"),
		("Root Domain", "domain_provider"),
		("Root Domain", "tls_provider"),
		("TLS Certificate", "tls_provider"),
	):
		if frappe.db.exists("DocType", doctype) and frappe.db.has_column(doctype, column):
			frappe.db.sql_ddl(f"ALTER TABLE `tab{doctype}` DROP COLUMN `{column}`")
	# Atlas Settings is a Single — its `provider` value lives in `tabSingles`, not a
	# column. Delete the stale row so the dropped field leaves no orphan value.
	frappe.db.delete("Singles", {"doctype": "Atlas Settings", "field": "provider"})


def _delete_dead_doctypes() -> None:
	for doctype in ("Provider", "Domain Provider", "TLS Provider"):
		if frappe.db.exists("DocType", doctype):
			frappe.delete_doc("DocType", doctype, force=True, ignore_permissions=True)
