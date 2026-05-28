"""Unit tests for `DigitalOceanProvider`.

These tests mock `DigitalOceanClient` exactly as the old
`test_server_provider.py` did — the provider class is the seam between
business logic and the HTTP wrapper, so mocking the client keeps the
tests fast and offline.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.providers.base import (
	Networking,
	ProvisionRequest,
	SshKey,
)


def _build_provider(monthly_cost=None):
	"""Build a DigitalOceanProvider with stubbed Settings and client."""
	from atlas.atlas.providers import digitalocean as do_module

	settings = SimpleNamespace(
		region="blr1",
		default_size="DigitalOcean/s-2vcpu-4gb-intel",
		default_image="DigitalOcean/ubuntu-24-04-x64",
	)
	with patch.object(frappe, "get_single", return_value=settings), \
			patch.object(do_module, "get_secret", return_value="dop_v1_fake"), \
			patch.object(do_module, "DigitalOceanClient") as client_cls:
		provider = do_module.DigitalOceanProvider()
		provider.client = MagicMock()
		client_cls.return_value = provider.client
	return provider


class TestDigitalOceanProviderAuthenticate(IntegrationTestCase):
	def test_authenticate_ok(self) -> None:
		provider = _build_provider()
		provider.client.verify_credentials.return_value = {
			"email": "ok@example.com",
			"rate_limit": 5000,
			"rate_remaining": 4998,
		}
		result = provider.authenticate()
		self.assertTrue(result.ok)
		self.assertEqual(result.account_label, "ok@example.com")
		self.assertEqual(result.rate_limit, 5000)
		self.assertEqual(result.rate_remaining, 4998)

	def test_authenticate_bad_returns_error_without_raising(self) -> None:
		from atlas.atlas.digitalocean import DigitalOceanError

		provider = _build_provider()
		provider.client.verify_credentials.side_effect = DigitalOceanError("GET /account -> 401")
		result = provider.authenticate()
		self.assertFalse(result.ok)
		self.assertIn("401", result.error)


class TestDigitalOceanProviderDiscover(IntegrationTestCase):
	def test_discover_returns_known_catalog(self) -> None:
		from atlas.atlas.providers import digitalocean as do_module

		provider = _build_provider()
		caps = provider.discover()
		self.assertEqual(len(caps.sizes), len(do_module.KNOWN_DIGITALOCEAN_SIZES))
		self.assertEqual(len(caps.images), len(do_module.KNOWN_DIGITALOCEAN_IMAGES))
		# Cost lookup matches the constant.
		first = caps.sizes[0]
		self.assertEqual(
			first.monthly_cost_usd,
			do_module.DIGITALOCEAN_MONTHLY_COST_USD.get(first.slug),
		)


class TestDigitalOceanProviderProvision(IntegrationTestCase):
	def test_provision_strips_prefix_and_returns_partial_result(self) -> None:
		provider = _build_provider()
		provider.client.create_droplet.return_value = {"id": 12345, "status": "new"}
		request = ProvisionRequest(
			title="atlas-srv-1",
			size="DigitalOcean/s-2vcpu-4gb-intel",
			image="DigitalOcean/ubuntu-24-04-x64",
			ssh_key=SshKey(vendor_id="fp:fingerprint"),
			networking=Networking.DUAL_STACK,
			tags=("atlas", "atlas-srv-1"),
		)
		result = provider.provision(request)
		_, kwargs = provider.client.create_droplet.call_args
		self.assertEqual(kwargs["size"], "s-2vcpu-4gb-intel")
		self.assertEqual(kwargs["image"], "ubuntu-24-04-x64")
		self.assertEqual(kwargs["region"], "blr1")
		self.assertEqual(kwargs["ssh_key_ids"], ["fp:fingerprint"])
		self.assertEqual(kwargs["tags"], ["atlas", "atlas-srv-1"])
		self.assertEqual(result.provider_resource_id, "12345")
		self.assertEqual(result.size, "DigitalOcean/s-2vcpu-4gb-intel")
		self.assertFalse(result.ready)
		self.assertEqual(result.provider_metadata, {"id": 12345, "status": "new"})


class TestDigitalOceanProviderDescribe(IntegrationTestCase):
	def test_describe_returns_partial_when_not_active(self) -> None:
		provider = _build_provider()
		provider.client.get_droplet.return_value = {
			"id": 999,
			"status": "new",
			"size_slug": "s-2vcpu-4gb-intel",
			"image": {"slug": "ubuntu-24-04-x64"},
		}
		result = provider.describe("999")
		self.assertFalse(result.ready)
		self.assertEqual(result.size, "DigitalOcean/s-2vcpu-4gb-intel")
		self.assertEqual(result.image, "DigitalOcean/ubuntu-24-04-x64")

	def test_describe_returns_ready_with_networking_when_active(self) -> None:
		provider = _build_provider()
		provider.client.get_droplet.return_value = {
			"id": 4242,
			"status": "active",
			"size_slug": "s-2vcpu-4gb-intel",
			"image": {"slug": "ubuntu-24-04-x64"},
			"networks": {
				"v4": [{"type": "public", "ip_address": "5.6.7.8"}],
				"v6": [{"type": "public", "ip_address": "2a03:b0c0:abcd:5678::1", "netmask": 64}],
			},
		}
		result = provider.describe("4242")
		self.assertTrue(result.ready)
		self.assertEqual(result.networking.ipv4_address, "5.6.7.8")
		self.assertEqual(result.networking.ipv6_address, "2a03:b0c0:abcd:5678::1")
		self.assertEqual(result.networking.ipv6_prefix, "2a03:b0c0:abcd:5678::/64")
		self.assertTrue(result.networking.ipv6_virtual_machine_range.endswith("/124"))


class TestDigitalOceanProviderDestroy(IntegrationTestCase):
	def test_destroy_calls_delete_droplet(self) -> None:
		provider = _build_provider()
		provider.destroy("12345")
		provider.client.delete_droplet.assert_called_once_with(12345)
