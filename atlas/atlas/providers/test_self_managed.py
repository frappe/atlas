"""Unit tests for `SelfManagedProvider`."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.providers.base import (
	Networking,
	ProvisionRequest,
	ServerNetworking,
	SshKey,
)
from atlas.atlas.providers.self_managed import SelfManagedProvider


class TestSelfManagedProvider(IntegrationTestCase):
	def test_authenticate_returns_ok(self) -> None:
		result = SelfManagedProvider().authenticate()
		self.assertTrue(result.ok)
		self.assertEqual(result.account_label, "local")

	def test_discover_returns_empty_capabilities(self) -> None:
		caps = SelfManagedProvider().discover()
		self.assertEqual(caps.sizes, ())
		self.assertEqual(caps.images, ())

	def test_provision_throws_without_prebuilt_networking(self) -> None:
		request = ProvisionRequest(
			title="atlas-sm-1",
			size="",
			image="",
			ssh_key=SshKey(),
			networking=Networking.DUAL_STACK,
		)
		with self.assertRaises(frappe.ValidationError):
			SelfManagedProvider().provision(request)

	def test_provision_returns_ready_result_with_networking(self) -> None:
		prebuilt = ServerNetworking(
			ipv4_address="203.0.113.10",
			ipv6_address="2001:db8::1",
			ipv6_prefix="2001:db8::/64",
			ipv6_virtual_machine_range="2001:db8:dead::/124",
		)
		request = ProvisionRequest(
			title="atlas-sm-1",
			size="",
			image="",
			ssh_key=SshKey(),
			networking=Networking.DUAL_STACK,
			prebuilt_networking=prebuilt,
		)
		result = SelfManagedProvider().provision(request)
		self.assertTrue(result.ready)
		self.assertEqual(result.networking, prebuilt)
		self.assertEqual(result.provider_resource_id, "")

	def test_describe_reads_server_row(self) -> None:
		fake_server = SimpleNamespace(
			ipv4_address="203.0.113.20",
			ipv6_address="2001:db8::2",
			ipv6_prefix="2001:db8::/64",
			ipv6_virtual_machine_range="2001:db8:beef::/124",
		)
		with patch.object(frappe, "get_doc", return_value=fake_server):
			result = SelfManagedProvider().describe("some-server-uuid")
		self.assertTrue(result.ready)
		self.assertEqual(result.networking.ipv4_address, "203.0.113.20")
		self.assertEqual(result.networking.ipv6_virtual_machine_range, "2001:db8:beef::/124")

	def test_destroy_is_noop(self) -> None:
		# No exception, no return.
		self.assertIsNone(SelfManagedProvider().destroy("anything"))
