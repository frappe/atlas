"""The user-facing SPA is served at /dashboard.

These tests pin the routing + auth front door without a browser:
- the `website_route_rules` hook maps /dashboard/<path> to the dashboard page;
- the dashboard www page bounces Guests to login and serves logged-in users.

Row-level access (own machines only) is a permissions concern covered in
test_permissions.py; here we only assert the route + the signed-in gate.
"""

import frappe
from frappe.tests import IntegrationTestCase


class TestDashboardRoute(IntegrationTestCase):
	def test_route_rule_registered(self) -> None:
		rules = frappe.get_hooks("website_route_rules")
		from_routes = {rule["from_route"] for rule in rules}
		self.assertIn(
			"/dashboard/<path:app_path>",
			from_routes,
			"the dashboard SPA route rule must be registered",
		)
		dashboard_rules = [r for r in rules if r["from_route"].startswith("/dashboard")]
		for rule in dashboard_rules:
			self.assertEqual(rule["to_route"], "dashboard")

	def test_guest_is_redirected_to_login(self) -> None:
		from atlas.www import dashboard

		original = frappe.session.user
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.Redirect):
				dashboard.get_context(frappe._dict())
			self.assertEqual(
				frappe.local.flags.redirect_location,
				"/login?redirect-to=/dashboard",
			)
		finally:
			frappe.set_user(original)

	def test_logged_in_user_gets_context(self) -> None:
		from atlas.www import dashboard

		# Administrator stands in for any signed-in user here — the page guard
		# only checks "not Guest"; role-scoping is enforced at the API layer.
		context = frappe._dict()
		result = dashboard.get_context(context)
		self.assertTrue(result.get("csrf_token") is not None)
