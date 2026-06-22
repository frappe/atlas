import os

import frappe
from frappe.sessions import get_csrf_token

# Require a logged-in user to view the dashboard SPA. Guests are bounced to
# the standard Frappe login with a redirect back. Row-level access (own
# machines only) is enforced by the DocType permissions + permission query
# conditions the SPA's API calls go through — this guard is only the
# front-door "must be signed in" check.
no_cache = 1

# The Vite build emits the SPA shell (hashed asset references + Frappe boot
# data) here. We inline it so the route is hash-agnostic: a rebuild changes
# the asset names but not this path.
SPA_INDEX = os.path.join(frappe.get_app_path("atlas"), "public", "frontend", "index.html")


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/dashboard"
		raise frappe.Redirect
	# The built shell carries the frappe-ui `{% for key in boot %}` block that
	# writes boot values onto `window`. We render it here with the boot context
	# rather than handing the raw string to the host page: a `{{ spa_index }}`
	# in dashboard.html would HTML-escape the markup AND never expand the inner
	# Jinja tags (Jinja does not recursively render a variable's value). So we
	# expand it ourselves and the host page emits the finished HTML.
	#
	# `csrf_token` is the load-bearing boot value: without it on `window`, the
	# SPA's writes (insert, lifecycle run_doc_method) are rejected with
	# CSRFTokenError. `yarn dev` has no Jinja render, so the SPA resolves the
	# user itself there and relies on the test site's ignore_csrf for writes
	# (see src/data/session.js); this is the production path.
	boot = {
		"csrf_token": get_csrf_token(),
		"user": frappe.session.user,
		"site_name": frappe.local.site,
	}
	context.spa_index = _render_built_index(boot)
	return context


def _render_built_index(boot: dict) -> str | None:
	"""The built SPA shell with its boot block expanded, or None before
	`bench build --app atlas` has run."""
	try:
		# nosemgrep: frappe-security-file-traversal -- fixed SPA_INDEX path derived from the app dir, not untrusted web input
		with open(SPA_INDEX) as handle:
			shell = handle.read()
	except FileNotFoundError:
		return None
	# nosemgrep: frappe-ssti -- template is the app's own built SPA shell read from disk, not user input
	return frappe.render_template(shell, {"boot": boot})
