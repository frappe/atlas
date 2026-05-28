"""Unit tests for the provider worker."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas.providers import worker
from atlas.atlas.providers.base import ProvisionResult, ServerNetworking


def _result(ready: bool, with_networking: bool = False, with_metadata: bool = False) -> ProvisionResult:
	networking = None
	if with_networking:
		networking = ServerNetworking(
			ipv4_address="5.6.7.8",
			ipv6_address="2a03:b0c0:abcd:5678::1",
			ipv6_prefix="2a03:b0c0:abcd:5678::/64",
			ipv6_virtual_machine_range="2a03:b0c0:abcd:5678::/124",
		)
	metadata = {"id": 1234, "status": "active"} if with_metadata else None
	return ProvisionResult(
		provider_resource_id="1234",
		size="DigitalOcean/s-2vcpu-4gb-intel",
		image="DigitalOcean/ubuntu-24-04-x64",
		ready=ready,
		networking=networking,
		provider_metadata=metadata,
	)


class TestWaitUntilReady(IntegrationTestCase):
	def test_returns_on_first_ready(self) -> None:
		provider = MagicMock()
		provider.describe.return_value = _result(ready=True)
		with patch.object(worker.time, "sleep"):
			result = worker.wait_until_ready(provider, "1234", timeout_seconds=60)
		self.assertTrue(result.ready)

	def test_polls_until_ready(self) -> None:
		provider = MagicMock()
		provider.describe.side_effect = [_result(ready=False), _result(ready=False), _result(ready=True)]
		with patch.object(worker.time, "sleep"):
			result = worker.wait_until_ready(provider, "1234", timeout_seconds=60)
		self.assertTrue(result.ready)
		self.assertEqual(provider.describe.call_count, 3)

	def test_times_out(self) -> None:
		provider = MagicMock()
		provider.describe.return_value = _result(ready=False)
		with patch.object(worker.time, "sleep"), \
				patch.object(worker.time, "monotonic", side_effect=[0, 1, 9999]):
			with self.assertRaises(frappe.ValidationError):
				worker.wait_until_ready(provider, "1234", timeout_seconds=60)


class TestApplyDescribeResult(IntegrationTestCase):
	def test_writes_networking_fields(self) -> None:
		server = SimpleNamespace(
			ipv4_address=None, ipv6_address=None, ipv6_prefix=None,
			ipv6_virtual_machine_range=None, size=None, image=None,
			provider_metadata=None,
		)
		worker._apply_describe_result(server, _result(ready=True, with_networking=True))
		self.assertEqual(server.ipv4_address, "5.6.7.8")
		self.assertEqual(server.ipv6_address, "2a03:b0c0:abcd:5678::1")

	def test_writes_provider_metadata_as_json_string(self) -> None:
		server = SimpleNamespace(
			ipv4_address=None, ipv6_address=None, ipv6_prefix=None,
			ipv6_virtual_machine_range=None, size=None, image=None,
			provider_metadata=None,
		)
		worker._apply_describe_result(server, _result(ready=True, with_metadata=True))
		self.assertEqual(json.loads(server.provider_metadata), {"id": 1234, "status": "active"})

	def test_skips_empty_size_image(self) -> None:
		# Self-Managed describe() returns size="" and image=""; the writer
		# should not overwrite the Server's existing (likely empty) values
		# with an empty string just to keep them empty.
		server = SimpleNamespace(
			ipv4_address=None, ipv6_address=None, ipv6_prefix=None,
			ipv6_virtual_machine_range=None,
			size="prev-size", image="prev-image",
			provider_metadata=None,
		)
		empty_result = ProvisionResult(
			provider_resource_id="", size="", image="", ready=True, networking=None,
		)
		worker._apply_describe_result(server, empty_result)
		self.assertEqual(server.size, "prev-size")
		self.assertEqual(server.image, "prev-image")
