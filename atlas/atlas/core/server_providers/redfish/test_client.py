from __future__ import annotations

from unittest.mock import Mock, patch

import requests
from frappe.tests import UnitTestCase

from atlas.atlas.core.server_providers.redfish.client import RedfishClient, RedfishError

ROOT = "http://bmc.example/redfish/v1"
SYSTEM_PATH = "/redfish/v1/Systems/machine-1"
SYSTEM = {
	"@odata.type": "#ComputerSystem.v1_20_0.ComputerSystem",
	"@odata.id": SYSTEM_PATH,
	"Id": "machine-1",
	"Name": "Rack server",
	"UUID": "machine-uuid",
}
COLLECTION = {
	"@odata.type": "#ComputerSystemCollection.ComputerSystemCollection",
	"Members": [{"@odata.id": SYSTEM_PATH}],
}


def response(resource: object, status: int = 200) -> Mock:
	return Mock(status_code=status, json=Mock(return_value=resource))


class TestRedfishClient(UnitTestCase):
	def test_discovers_a_system_through_service_links(self) -> None:
		root = {
			"@odata.type": "#ServiceRoot.v1_15_0.ServiceRoot",
			"Systems": {"@odata.id": "/redfish/v1/Systems"},
		}
		with patch(
			"requests.get", side_effect=[response(root), response(COLLECTION), response(SYSTEM)]
		) as read:
			system = RedfishClient("HTTP://BMC.EXAMPLE:80/").discover_system()

		self.assertEqual(system.url, "http://bmc.example" + SYSTEM_PATH)
		self.assertEqual((system.id, system.name, system.uuid), ("machine-1", "Rack server", "machine-uuid"))
		self.assertEqual(
			[call.args[0] for call in read.call_args_list], [ROOT, ROOT + "/Systems", system.url]
		)
		for call in read.call_args_list:
			self.assertFalse(call.kwargs["allow_redirects"])
			self.assertEqual(call.kwargs["timeout"], 10)

	def test_reads_a_direct_system_with_the_supplied_credentials(self) -> None:
		with patch("requests.get", return_value=response(SYSTEM)) as read:
			RedfishClient("http://bmc.example" + SYSTEM_PATH, "operator", "test-password").discover_system()

		read.assert_called_once()
		self.assertEqual(read.call_args.kwargs["auth"], ("operator", "test-password"))
		self.assertNotIn("verify", read.call_args.kwargs)

	def test_rejects_ambiguous_and_empty_collections(self) -> None:
		for members in ([], COLLECTION["Members"] * 2, None, ["invalid"]):
			with (
				self.subTest(members=members),
				patch("requests.get", return_value=response({**COLLECTION, "Members": members})) as read,
			):
				with self.assertRaisesRegex(RedfishError, "specific system URL"):
					RedfishClient(ROOT + "/Systems").discover_system()
				read.assert_called_once()

	def test_rejects_invalid_urls_before_any_request(self) -> None:
		for url in (
			"",
			"file:///redfish/v1",
			"http://bmc.example:bad/redfish/v1",
			"http://user:secret@bmc.example/redfish/v1",
			ROOT + "?password=secret",
			ROOT + "#fragment",
			"http://bmc.example/other",
		):
			with self.subTest(url=url), patch("requests.get") as read:
				with self.assertRaises(RedfishError):
					RedfishClient(url)
				read.assert_not_called()

	def test_requires_a_complete_credential_pair(self) -> None:
		for username, password in (("operator", ""), ("", "test-password")):
			with self.assertRaisesRegex(RedfishError, "both"):
				RedfishClient(ROOT, username, password)

	def test_rejects_cross_origin_links_without_forwarding_credentials(self) -> None:
		for link in (
			"https://bmc.example" + SYSTEM_PATH,
			"http://other.example" + SYSTEM_PATH,
			"//other.example" + SYSTEM_PATH,
		):
			with (
				self.subTest(link=link),
				patch(
					"requests.get", return_value=response({**COLLECTION, "Members": [{"@odata.id": link}]})
				) as read,
			):
				with self.assertRaisesRegex(RedfishError, "configured service"):
					RedfishClient(ROOT, "operator", "test-password").discover_system()
				read.assert_called_once()

	def test_reports_http_and_transport_errors_without_remote_details(self) -> None:
		for status in (302, 401, 403, 404, 500):
			with (
				self.subTest(status=status),
				patch("requests.get", return_value=response({"secret": "remote-detail"}, status)),
			):
				with self.assertRaisesRegex(RedfishError, f"HTTP {status}"):
					RedfishClient(ROOT).discover_system()
		with patch("requests.get", side_effect=requests.ConnectionError("test-password")):
			with self.assertRaisesRegex(RedfishError, "Could not connect") as raised:
				RedfishClient(ROOT).discover_system()
		self.assertNotIn("test-password", str(raised.exception))

	def test_rejects_malformed_resources(self) -> None:
		for resource in (
			[],
			{},
			{**SYSTEM, "Id": 42},
			{**SYSTEM, "Name": ""},
			{**SYSTEM, "UUID": 42},
			{**SYSTEM, "@odata.id": None},
		):
			with self.subTest(resource=resource), patch("requests.get", return_value=response(resource)):
				with self.assertRaises(RedfishError):
					RedfishClient(ROOT).discover_system()
		with patch("requests.get", return_value=Mock(status_code=200, json=Mock(side_effect=ValueError))):
			with self.assertRaisesRegex(RedfishError, "invalid JSON"):
				RedfishClient(ROOT).discover_system()
