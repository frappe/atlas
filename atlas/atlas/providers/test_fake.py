"""Unit tests for `FakeProvider` (the ABC half of the dev fake provider)."""

from __future__ import annotations

import ipaddress

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.providers.base import Networking, ProvisionRequest, SshKey
from atlas.atlas.providers.fake import (
	DEFAULT_FAKE_IMAGE,
	DEFAULT_FAKE_SIZE,
	FakeProvider,
	require_developer_mode,
)


def _request(title: str = "fake-srv-1", size: str = "", image: str = "") -> ProvisionRequest:
	return ProvisionRequest(
		title=title,
		size=size,
		image=image,
		ssh_key=SshKey(),
		networking=Networking.DUAL_STACK,
	)


class TestFakeProvider(IntegrationTestCase):
	def setUp(self) -> None:
		self._developer_mode = frappe.local.conf.developer_mode
		frappe.local.conf.developer_mode = 1

	def tearDown(self) -> None:
		frappe.local.conf.developer_mode = self._developer_mode

	def test_authenticate_ok(self) -> None:
		result = FakeProvider().authenticate()
		self.assertTrue(result.ok)
		self.assertEqual(result.account_label, "fake")

	def test_discover_returns_catalog(self) -> None:
		caps = FakeProvider().discover()
		self.assertTrue(caps.sizes)
		self.assertTrue(caps.images)
		self.assertIn("fake-2vcpu-4gb", {size.slug for size in caps.sizes})

	def test_provision_is_ready_immediately(self) -> None:
		result = FakeProvider().provision(_request())
		self.assertTrue(result.ready)
		self.assertTrue(result.provider_resource_id.startswith("fake-"))
		self.assertIsNotNone(result.networking)

	def test_provision_defaults_size_and_image_when_blank(self) -> None:
		result = FakeProvider().provision(_request(size="", image=""))
		self.assertEqual(result.size, DEFAULT_FAKE_SIZE)
		self.assertEqual(result.image, DEFAULT_FAKE_IMAGE)

	def test_provision_honors_explicit_size_and_image(self) -> None:
		result = FakeProvider().provision(_request(size="Fake/fake-8vcpu-16gb", image="Fake/debian-12"))
		self.assertEqual(result.size, "Fake/fake-8vcpu-16gb")
		self.assertEqual(result.image, "Fake/debian-12")

	def test_networking_is_deterministic_per_title(self) -> None:
		first = FakeProvider().provision(_request("same-title")).networking
		second = FakeProvider().provision(_request("same-title")).networking
		self.assertEqual(first, second)

	def test_networking_differs_between_titles(self) -> None:
		one = FakeProvider().provision(_request("title-a")).networking
		two = FakeProvider().provision(_request("title-b")).networking
		self.assertNotEqual(one.ipv4_address, two.ipv4_address)

	def test_networking_addresses_are_unroutable(self) -> None:
		net = FakeProvider().provision(_request()).networking
		# IPv4 in TEST-NET-3 (203.0.113.0/24); IPv6 under the documentation 2001:db8::/32.
		self.assertIn(ipaddress.ip_address(net.ipv4_address), ipaddress.ip_network("203.0.113.0/24"))
		self.assertIn(ipaddress.ip_address(net.ipv6_address), ipaddress.ip_network("2001:db8::/32"))
		self.assertTrue(net.ipv6_virtual_machine_range.endswith("/124"))

	def test_describe_is_ready_and_self_consistent(self) -> None:
		provisioned = FakeProvider().provision(_request("descr"))
		described = FakeProvider().describe(provisioned.provider_resource_id)
		self.assertTrue(described.ready)
		self.assertEqual(described.networking, provisioned.networking)

	def test_list_servers_returns_canned_inventory(self) -> None:
		from atlas.atlas.providers.fake import FAKE_DISCOVERED_TITLES

		discovered = FakeProvider().list_servers()
		self.assertEqual(len(discovered), len(FAKE_DISCOVERED_TITLES))
		titles = {server.title for server in discovered}
		self.assertEqual(titles, set(FAKE_DISCOVERED_TITLES))
		# Every id uses the fake-<token> shape provision() mints.
		for server in discovered:
			self.assertTrue(server.provider_resource_id.startswith("fake-"))
			self.assertEqual(server.size, DEFAULT_FAKE_SIZE)

	def test_list_servers_ids_resolve_via_describe(self) -> None:
		"""The canned ids round-trip: describe(id) is ready and its IPv4 matches the
		preview row's, so import's authoritative describe() agrees with the picker."""
		for server in FakeProvider().list_servers():
			described = FakeProvider().describe(server.provider_resource_id)
			self.assertTrue(described.ready)
			self.assertEqual(described.networking.ipv4_address, server.ipv4_address)

	def test_destroy_is_noop(self) -> None:
		self.assertIsNone(FakeProvider().destroy("fake-anything"))

	def test_allocate_reserved_ip_is_unroutable(self) -> None:
		# Reserved IPs draw from TEST-NET-2 (a different documentation block than
		# the servers' TEST-NET-3) so they never collide with a host's endpoint.
		reserved = FakeProvider().allocate_reserved_ip()
		self.assertIn(ipaddress.ip_address(reserved.ip_address), ipaddress.ip_network("198.51.100.0/24"))

	def test_reserved_ip_assign_unassign_release_list_are_noops(self) -> None:
		provider = FakeProvider()
		self.assertIsNone(provider.assign_reserved_ip("fake-rip", "fake-host"))
		self.assertIsNone(provider.unassign_reserved_ip("fake-rip"))
		self.assertIsNone(provider.release_reserved_ip("fake-rip"))
		self.assertEqual(provider.list_reserved_ips(), ())


class TestFakeProviderDeveloperModeGate(IntegrationTestCase):
	"""Mutating methods refuse to run when developer_mode is off.

	`frappe.conf` is a LocalProxy, so `patch.dict`/`patch.object` can't restore
	it cleanly; toggle `frappe.local.conf.developer_mode` and restore in
	tearDown (the idiom in frappe.tests.test_modules)."""

	def setUp(self) -> None:
		self._developer_mode = frappe.local.conf.developer_mode

	def tearDown(self) -> None:
		frappe.local.conf.developer_mode = self._developer_mode

	def test_require_developer_mode_passes_when_on(self) -> None:
		frappe.local.conf.developer_mode = 1
		require_developer_mode()  # no throw

	def test_require_developer_mode_throws_when_off(self) -> None:
		frappe.local.conf.developer_mode = 0
		with self.assertRaises(frappe.ValidationError):
			require_developer_mode()

	def test_provision_throws_when_developer_mode_off(self) -> None:
		frappe.local.conf.developer_mode = 0
		with self.assertRaises(frappe.ValidationError):
			FakeProvider().provision(_request())

	def test_authenticate_throws_when_developer_mode_off(self) -> None:
		frappe.local.conf.developer_mode = 0
		with self.assertRaises(frappe.ValidationError):
			FakeProvider().authenticate()

	def test_allocate_reserved_ip_throws_when_developer_mode_off(self) -> None:
		frappe.local.conf.developer_mode = 0
		with self.assertRaises(frappe.ValidationError):
			FakeProvider().allocate_reserved_ip()


class TestFakeProviderRegistered(IntegrationTestCase):
	def test_registry_resolves_fake(self) -> None:
		from atlas.atlas import providers

		providers._load_implementations()
		self.assertIn("Fake", providers._REGISTRY)
		self.assertIs(providers._REGISTRY["Fake"], FakeProvider)
