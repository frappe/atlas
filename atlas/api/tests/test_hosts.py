from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from atlas.api.routes.hosts import grant_host_access, list_hosts, revoke_host_access
from atlas.api.tests.test_support import TENANT_ID, api_request, call_route

ROUTES = "atlas.api.routes.hosts"


class TestHostRoutes(UnitTestCase):
	def test_hosts_need_tenant_zero(self) -> None:
		for tenant_id in (TENANT_ID, None):
			with api_request("GET", "/api/atlas/hosts", tenant_id=tenant_id, send_tenant_header=True):
				status, body = call_route(list_hosts)

			self.assertIn(status, (400, 403), body)

	def test_hosts_list_the_hosts_with_their_tags(self) -> None:
		rows = [frappe._dict(name="server-2", title="osa-2", status="Running")]
		with (
			api_request("GET", "/api/atlas/hosts", tenant_id=0, send_tenant_header=True),
			patch(f"{ROUTES}.frappe.get_all", return_value=rows),
			patch(f"{ROUTES}.read_tags_for", return_value={"server-2": {"rack": "r1"}}),
		):
			status, body = call_route(list_hosts)

		self.assertEqual(status, 200)
		self.assertEqual(
			body["items"], [{"id": "server-2", "title": "osa-2", "status": "running", "tags": {"rack": "r1"}}]
		)

	def test_grant_and_revoke_reach_host_access(self) -> None:
		with (
			api_request(
				"POST",
				"/api/atlas/hosts/server-2/access/grant",
				tenant_id=0,
				send_tenant_header=True,
				json={"email": "Alice@frappe.io", "expires_at": "2026-10-02T14:00:00Z"},
			),
			patch(f"{ROUTES}.HostAccess") as host_access,
		):
			host_access.return_value.grant.return_value = "alice@frappe.io"
			status, body = call_route(grant_host_access, host_id="server-2")

		self.assertEqual(status, 200)
		self.assertEqual(body["email"], "alice@frappe.io")
		host_access.assert_called_once_with("server-2")
		host_access.return_value.grant.assert_called_once()

		with (
			api_request(
				"POST",
				"/api/atlas/hosts/all/access/revoke",
				tenant_id=0,
				send_tenant_header=True,
				json={"email": "alice@frappe.io"},
			),
			patch(f"{ROUTES}.HostAccess") as host_access,
		):
			status, _ = call_route(revoke_host_access, host_id="all")

		self.assertEqual(status, 204)
		host_access.return_value.revoke.assert_called_once_with("alice@frappe.io")

	def test_another_tenant_cannot_grant(self) -> None:
		with (
			api_request(
				"POST",
				"/api/atlas/hosts/all/access/grant",
				tenant_id=TENANT_ID,
				send_tenant_header=True,
				json={"email": "alice@frappe.io", "expires_at": "2026-10-02T14:00:00Z"},
			),
			patch(f"{ROUTES}.HostAccess") as host_access,
		):
			status, _ = call_route(grant_host_access, host_id="all")

		self.assertEqual(status, 403)
		host_access.assert_not_called()
