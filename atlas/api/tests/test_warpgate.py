from __future__ import annotations

from unittest.mock import patch

from frappe.tests import UnitTestCase

from atlas.api.routes.warpgate import close_warpgate_sessions
from atlas.api.tests.test_support import TENANT_ID, api_request, call_route

ROUTES = "atlas.api.routes.warpgate"


class TestWarpgateRoutes(UnitTestCase):
	def test_closing_sessions_needs_tenant_zero(self) -> None:
		with (
			api_request(
				"POST",
				"/api/atlas/warpgate/sessions/close",
				tenant_id=TENANT_ID,
				send_tenant_header=True,
				json={"email": "alice@frappe.io"},
			),
			patch(f"{ROUTES}.close_sessions") as close_sessions,
		):
			status, body = call_route(close_warpgate_sessions)

		self.assertIn(status, (400, 403), body)
		close_sessions.assert_not_called()

	def test_closing_sessions_reaches_warpgate(self) -> None:
		with (
			api_request(
				"POST",
				"/api/atlas/warpgate/sessions/close",
				tenant_id=0,
				send_tenant_header=True,
				json={"email": "alice@frappe.io"},
			),
			patch(f"{ROUTES}.close_sessions") as close_sessions,
		):
			status, _ = call_route(close_warpgate_sessions)

		self.assertEqual(status, 204)
		close_sessions.assert_called_once_with("alice@frappe.io")
