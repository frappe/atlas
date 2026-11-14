from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from atlas.api.routes.beacon import BeaconResourceList, list_beacon_resources
from atlas.api.tests.test_support import TENANT_ID, api_request, call_route, route_response

ROUTES = "atlas.api.routes.beacon"
ATLAS = {
	"resource_id": "atlas",
	"resource_type": "Atlas",
	"parent_id": "",
	"title": "Atlas",
	"status": "Active",
}


class TestBeaconRoutes(UnitTestCase):
	def test_a_tenant_cannot_list_the_resources(self) -> None:
		with api_request("GET", "/api/atlas/beacon/resources", tenant_id=TENANT_ID, send_tenant_header=True):
			status, body = call_route(list_beacon_resources)

		self.assertEqual(status, 403, body)

	def test_the_list_starts_with_atlas_and_a_matching_etag_returns_not_modified(self) -> None:
		with api_request("GET", "/api/atlas/beacon/resources"):
			status, body = call_route(list_beacon_resources)
			etag = route_response(list_beacon_resources).headers["ETag"]

		self.assertEqual(status, 200)
		self.assertEqual(body["resources"][0], ATLAS)

		with api_request("GET", "/api/atlas/beacon/resources", headers={"If-None-Match": etag}):
			self.assertEqual(route_response(list_beacon_resources).status_code, 304)

	def test_statuses_map_to_beacon_statuses(self) -> None:
		hosts = [frappe._dict(name="server-1", title="waw-1", status="Failed")]
		with patch(f"{ROUTES}.frappe.get_all", return_value=hosts):
			resources = BeaconResourceList()
			resources.add_hosts()

		self.assertEqual(resources.rows[0]["status"], "Suspended")
		self.assertEqual(resources.rows[0]["title"], "waw-1")
