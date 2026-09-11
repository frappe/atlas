from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase

from atlas.api.models import IPAddressResponse
from atlas.api.routes.ip_addresses import (
	get_ip_address,
	list_ip_addresses,
	release_ip_address,
	reserve_ip_address,
)
from atlas.api.tests.test_support import TENANT_ID, api_request, call_route


def build_ip_address(tenant_id: int = TENANT_ID, **overrides) -> SimpleNamespace:
	"""Return one stored public IPv4 address row."""
	values = {
		"name": "203.0.113.10",
		"tenant_id": tenant_id,
		"address": "203.0.113.10",
		"status": "Allocated",
		"virtual_machine": None,
		"creation": "2026-09-08 10:00:00",
		"provider_resource_id": "provider-1",
		"server": "node-1",
	}
	values.update(overrides)
	return SimpleNamespace(**values)


def owned_document(ip_address: SimpleNamespace):
	"""Patch the ownership lookup so it returns one IP address."""
	return patch("atlas.api.routes.ip_addresses.get_owned_document", return_value=ip_address)


class TestIPAddressView(UnitTestCase):
	def test_view_maps_the_status_and_hides_provider_data(self) -> None:
		view = IPAddressResponse.from_document(build_ip_address(status="Attached", virtual_machine="vm-1"))

		self.assertEqual(view.state, "attached")
		self.assertEqual(view.virtual_machine_id, "vm-1")
		self.assertIsInstance(view.created_at, int)
		self.assertNotIn("provider_resource_id", view.model_fields)
		self.assertNotIn("server", view.model_fields)


class TestReserveIPAddress(UnitTestCase):
	def reserve(self, body: dict):
		"""Run the reserve route against one request body."""
		with (
			api_request("POST", "/api/atlas/ip-addresses", tenant_id=TENANT_ID, json=body),
			patch(
				"atlas.api.routes.ip_addresses.reserve_for_tenant",
				return_value="203.0.113.10",
			) as reserve,
			patch("atlas.api.routes.ip_addresses.frappe.get_doc", return_value=build_ip_address()),
		):
			return (*call_route(reserve_ip_address), reserve)

	def test_pool_is_the_default_source(self) -> None:
		status, body, reserve = self.reserve({})

		self.assertEqual(status, 201)
		self.assertEqual(body["state"], "reserved")
		reserve.assert_called_once_with(TENANT_ID, "pool")

	def test_provider_source_is_accepted(self) -> None:
		_, _, reserve = self.reserve({"source": "provider"})

		reserve.assert_called_once_with(TENANT_ID, "provider")

	def test_an_unknown_source_is_rejected(self) -> None:
		status, body, _ = self.reserve({"source": "somewhere"})

		self.assertEqual(status, 400)
		self.assertEqual([item["name"] for item in body["error"]["fields"]], ["source"])


class TestReadIPAddresses(UnitTestCase):
	def test_list_filters_by_tenant(self) -> None:
		with (
			api_request("GET", "/api/atlas/ip-addresses", tenant_id=TENANT_ID),
			patch(
				"atlas.api.routes.ip_addresses.frappe.get_list", return_value=[build_ip_address()]
			) as get_list,
		):
			status, body = call_route(list_ip_addresses)

		self.assertEqual(status, 200)
		self.assertEqual(get_list.call_args.kwargs["filters"], {"tenant_id": TENANT_ID})
		self.assertFalse(body["has_more"])


class TestReleaseIPAddress(UnitTestCase):
	def release(self, ip_address: SimpleNamespace):
		"""Run the release route against one stored address."""
		ip_address.release_to_pool = Mock()
		with (
			api_request("DELETE", "/api/atlas/ip-addresses/203.0.113.10", tenant_id=TENANT_ID),
			owned_document(ip_address),
		):
			return (*call_route(release_ip_address, ip_address_id="203.0.113.10"), ip_address.release_to_pool)

	def test_release_returns_no_content(self) -> None:
		status, body, release = self.release(build_ip_address())

		self.assertEqual(status, 204)
		self.assertIsNone(body)
		release.assert_called_once()

	def test_an_attached_address_returns_a_conflict(self) -> None:
		from atlas.metal_server.core.ip_address_service import IPAddressInUse

		ip_address = build_ip_address(status="Attached", virtual_machine="vm-1")
		ip_address.release_to_pool = Mock(side_effect=IPAddressInUse("Address is in use."))
		with (
			api_request("DELETE", "/api/atlas/ip-addresses/203.0.113.10", tenant_id=TENANT_ID),
			owned_document(ip_address),
		):
			status, body = call_route(release_ip_address, ip_address_id="203.0.113.10")

		self.assertEqual(status, 409)
		self.assertEqual(body["error"]["code"], "conflict")
