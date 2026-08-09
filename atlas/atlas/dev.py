"""Developer conveniences for local Atlas sites — not for production.

Run:

    bench --site <site> execute atlas.atlas.dev.force_setup_complete

The Setup Wizard can't be finished the normal way on a throwaway site: Atlas's
wizard runs a `_stage_provider` task (see `atlas/setup.py`) that throws unless
it's handed a real `provider_type`, so `complete_setup_wizard` is a dead end for
scratch sites. This bypasses the wizard stages entirely and just flips the gate
flags so the desk loads.

`developer_mode`-gated. Idempotent.
"""

from __future__ import annotations

import frappe
from frappe import _


def force_setup_complete() -> None:
	"""Mark the Setup Wizard complete without running its stages.

	The authoritative gate is `frappe.is_setup_complete()`, which is True only
	when every `Installed Application` row (frappe/erpnext) has
	`is_setup_complete = 1`. We flip all installed apps, fill the System Settings
	fields the wizard would have set, then mirror Frappe by writing the computed
	`setup_complete` value back to System Settings."""
	if not frappe.conf.developer_mode:
		frappe.throw(_("force_setup_complete is only available when developer_mode is enabled"))

	# Mandatory System Settings fields the wizard fills; without them a plain
	# save raises MandatoryError, so set them directly.
	frappe.db.set_single_value(
		"System Settings",
		{"language": "en", "time_zone": "America/New_York"},
		update_modified=False,
	)

	# The real per-app gate. Mirror frappe's enable_setup_wizard_complete().
	for app in frappe.get_all("Installed Application", pluck="app_name"):
		frappe.db.set_value(
			"Installed Application", {"app_name": app}, "is_setup_complete", 1, update_modified=False
		)

	frappe.db.set_single_value(
		"System Settings", "setup_complete", frappe.is_setup_complete(), update_modified=False
	)

	# nosemgrep: frappe-manual-commit -- dev helper: persist the gate flip before clearing the cache
	frappe.db.commit()
	frappe.clear_cache()

	print(f"Setup marked complete — is_setup_complete() = {frappe.is_setup_complete()}")
