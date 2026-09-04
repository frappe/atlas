"""Serial console portal page. A guest may open it with a valid one-time token."""

from __future__ import annotations

import frappe

no_cache = 1


def get_context(context):
	context.no_cache = 1
	context.show_sidebar = False
	context.virtual_machine = frappe.form_dict.get("vm") or ""
	# The token is read from the URL fragment in the browser, never on the server.
	context.sitename = frappe.local.site
	context.socketio_port = frappe.get_common_site_config().get("socketio_port") or 9000
	return context
