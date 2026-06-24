"""Stop reading `atlas_*` site config as the setup input — seed the Singles once.

The explicit setup contract (`atlas/setup.py`) makes every value Atlas needs an
explicit field on a Settings Single, set through a typed `setup()` setter, never
read from `frappe.conf` at runtime. Existing benches still carry their `atlas_*`
site-config keys; this patch reads them ONE LAST TIME via `setup.from_site_config()`
and applies the setters so every Single is populated, then is done.

Bounded + safe to run unattended during `bench migrate`:

- No-op on a site with no `atlas_provider_type` key (a fresh install configured via
  the Setup Wizard, or one already migrated). Nothing to seed.
- No-op once `Atlas Settings.provider_type` is already set AND the active vendor's
  credential field is populated — the common case after the first migrate or a
  bootstrap run. Avoids the Scaleway setter's load-bearing `discover()` network call
  on every subsequent migrate.
- Best-effort: a failure (e.g. Scaleway `discover()` can't reach the API mid-migrate,
  or a key is half-set) is logged, not raised — a migrate must not hard-fail on a
  network blip. The operator finishes config in the wizard / on the Singles.

Does NOT delete the operator's `atlas_*` keys — it only stops reading them as the
input. (`region` is already handled by `move_region_to_atlas_settings`.)
"""

import frappe


def execute() -> None:
	if not frappe.conf.get("atlas_provider_type"):
		return  # no legacy keys — fresh/wizard-configured or already migrated
	if _already_seeded():
		return

	from atlas import setup

	try:
		setup.run(setup.from_site_config())
	except Exception:
		# A migrate must not hard-fail on a network blip (Scaleway discover) or a
		# half-set key. Log and leave it — the operator finishes in the wizard.
		frappe.log_error(title="seed_settings_from_site_config")


def _already_seeded() -> bool:
	"""True once provider_type + the active vendor's credential are populated.

	Keys off `Atlas Settings.provider_type`: DigitalOcean needs an api_token,
	Scaleway a secret_key (its setter's discover() is the expensive bit we skip on
	re-run), Self-Managed has no vendor credential so provider_type alone suffices."""
	provider_type = frappe.db.get_single_value("Atlas Settings", "provider_type")
	if not provider_type:
		return False
	if provider_type == "DigitalOcean":
		return bool(_single_value("DigitalOcean Settings", "api_token"))
	if provider_type == "Scaleway":
		return bool(_single_value("Scaleway Settings", "secret_key"))
	return True  # Self-Managed / Fake — no vendor credential to check


def _single_value(doctype: str, field: str) -> str | None:
	# order_by=None: tabSingles has no `creation` column.
	value = frappe.db.get_value("Singles", {"doctype": doctype, "field": field}, "value", order_by=None)
	return value or None
