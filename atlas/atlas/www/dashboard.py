import os

import frappe

# Require a logged-in user to view the dashboard SPA. Guests are bounced to
# the standard Frappe login with a redirect back. Row-level access (own
# machines only) is enforced by the DocType permissions + permission query
# conditions the SPA's API calls go through — this guard is only the
# front-door "must be signed in" check.
no_cache = 1

# The Vite build emits the SPA shell (hashed asset references + Frappe boot
# data) here. We inline it so the route is hash-agnostic: a rebuild changes
# the asset names but not this path.
SPA_INDEX = os.path.join(
	frappe.get_app_path("atlas"), "public", "frontend", "index.html"
)


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/dashboard"
		raise frappe.Redirect
	context.spa_index = _read_built_index()
	return context


def _read_built_index() -> str | None:
	"""The built SPA shell, or None before `bench build --app atlas` has run.

	The built file already carries the `{% for key in boot %}` block the
	frappe-ui plugin injects; returning it raw lets the www renderer expand it
	with the page's boot data."""
	try:
		with open(SPA_INDEX) as handle:
			return handle.read()
	except FileNotFoundError:
		return None
