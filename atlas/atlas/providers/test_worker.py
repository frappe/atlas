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
		with (
			patch.object(worker.time, "sleep"),
			patch.object(worker.time, "monotonic", side_effect=[0, 1, 9999]),
		):
			with self.assertRaises(frappe.ValidationError):
				worker.wait_until_ready(provider, "1234", timeout_seconds=60)


class TestApplyDescribeResult(IntegrationTestCase):
	def test_writes_networking_fields(self) -> None:
		server = SimpleNamespace(
			ipv4_address=None,
			ipv6_address=None,
			ipv6_prefix=None,
			ipv6_virtual_machine_range=None,
			size=None,
			image=None,
			provider_metadata=None,
		)
		worker._apply_describe_result(server, _result(ready=True, with_networking=True))
		self.assertEqual(server.ipv4_address, "5.6.7.8")
		self.assertEqual(server.ipv6_address, "2a03:b0c0:abcd:5678::1")

	def test_writes_provider_metadata_as_json_string(self) -> None:
		server = SimpleNamespace(
			ipv4_address=None,
			ipv6_address=None,
			ipv6_prefix=None,
			ipv6_virtual_machine_range=None,
			size=None,
			image=None,
			provider_metadata=None,
		)
		worker._apply_describe_result(server, _result(ready=True, with_metadata=True))
		self.assertEqual(json.loads(server.provider_metadata), {"id": 1234, "status": "active"})

	def test_skips_empty_size_image(self) -> None:
		# Self-Managed describe() returns size="" and image=""; the writer
		# should not overwrite the Server's existing (likely empty) values
		# with an empty string just to keep them empty.
		server = SimpleNamespace(
			ipv4_address=None,
			ipv6_address=None,
			ipv6_prefix=None,
			ipv6_virtual_machine_range=None,
			size="prev-size",
			image="prev-image",
			provider_metadata=None,
		)
		empty_result = ProvisionResult(
			provider_resource_id="",
			size="",
			image="",
			ready=True,
			networking=None,
		)
		worker._apply_describe_result(server, empty_result)
		self.assertEqual(server.size, "prev-size")
		self.assertEqual(server.image, "prev-image")


class TestEnqueueFinishProvisioning(IntegrationTestCase):
	def test_enqueues_when_no_job_in_flight(self) -> None:
		with (
			patch.object(worker.frappe, "enqueue") as enqueue,
			patch("frappe.utils.background_jobs.is_job_enqueued", return_value=False),
		):
			result = worker.enqueue_finish_provisioning("srv-1")
		self.assertTrue(result)
		enqueue.assert_called_once()
		_, kwargs = enqueue.call_args
		self.assertEqual(kwargs["server_name"], "srv-1")
		self.assertEqual(kwargs["job_id"], "finish_provisioning::srv-1")
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["queue"], "long")

	def test_skips_when_job_already_in_flight(self) -> None:
		# A job genuinely queued/running carries the stable id — don't stack a second.
		with (
			patch.object(worker.frappe, "enqueue") as enqueue,
			patch("frappe.utils.background_jobs.is_job_enqueued", return_value=True),
		):
			result = worker.enqueue_finish_provisioning("srv-1")
		self.assertFalse(result)
		enqueue.assert_not_called()


class TestReconcilePendingServers(IntegrationTestCase):
	def setUp(self) -> None:
		from atlas.tests.fixtures import make_provider, make_server

		self.provider = make_provider("test-provider-reconcile")
		self.make_server = make_server

	def _backdate(self, server_name: str, seconds: int) -> None:
		"""Push `modified` into the past so the row is past its grace window."""
		stale = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-seconds)
		frappe.db.set_value("Server", server_name, "modified", stale, update_modified=False)

	def test_re_enqueues_stale_pending_with_resource(self) -> None:
		server = self.make_server(
			self.provider, "reconcile-stale-pending", provider_resource_id="r1", status="Pending"
		)
		self._backdate(server.name, worker.RECONCILE_PENDING_GRACE_SECONDS + 60)
		with patch.object(worker, "enqueue_finish_provisioning", return_value=True) as enqueue:
			re_enqueued = worker.reconcile_pending_servers()
		# This row is among those swept (the shared test DB may hold other stale rows).
		self.assertIn(server.name, [c.args[0] for c in enqueue.call_args_list])
		self.assertIn(server.name, re_enqueued)

	def test_skips_fresh_pending(self) -> None:
		# A Pending row whose worker may still be progressing (recent modified) is
		# left alone — we key off staleness, not status, to avoid interrupting it.
		server = self.make_server(
			self.provider, "reconcile-fresh-pending", provider_resource_id="r2", status="Pending"
		)
		with patch.object(worker, "enqueue_finish_provisioning", return_value=True) as enqueue:
			re_enqueued = worker.reconcile_pending_servers()
		self.assertNotIn(server.name, [c.args[0] for c in enqueue.call_args_list])
		self.assertNotIn(server.name, re_enqueued)

	def test_skips_row_without_provider_resource_id(self) -> None:
		# No vendor id → provision() never recorded a resource; describe() would have
		# nothing to poll. Such a row failed earlier and is not this sweep's job.
		server = self.make_server(
			self.provider, "reconcile-no-resource", provider_resource_id=None, status="Pending"
		)
		self._backdate(server.name, worker.RECONCILE_PENDING_GRACE_SECONDS + 60)
		with patch.object(worker, "enqueue_finish_provisioning", return_value=True) as enqueue:
			worker.reconcile_pending_servers()
		self.assertNotIn(server.name, [c.args[0] for c in enqueue.call_args_list])

	def test_skips_active_server(self) -> None:
		server = self.make_server(
			self.provider, "reconcile-active", provider_resource_id="r3", status="Active"
		)
		self._backdate(server.name, worker.RECONCILE_BOOTSTRAPPING_GRACE_SECONDS + 60)
		with patch.object(worker, "enqueue_finish_provisioning", return_value=True) as enqueue:
			worker.reconcile_pending_servers()
		self.assertNotIn(server.name, [c.args[0] for c in enqueue.call_args_list])

	def test_bootstrapping_uses_longer_grace(self) -> None:
		# A Bootstrapping row stale past the (long) Pending window but within the
		# Bootstrapping window is still progressing legitimately — leave it alone.
		server = self.make_server(
			self.provider, "reconcile-bootstrapping-young", provider_resource_id="r4", status="Bootstrapping"
		)
		self._backdate(server.name, worker.RECONCILE_PENDING_GRACE_SECONDS + 60)
		with patch.object(worker, "enqueue_finish_provisioning", return_value=True) as enqueue:
			worker.reconcile_pending_servers()
		self.assertNotIn(server.name, [c.args[0] for c in enqueue.call_args_list])

	def test_does_not_double_enqueue_in_flight(self) -> None:
		# enqueue_finish_provisioning returns False when a job is already in flight;
		# the reconciler must not count it as re-enqueued.
		server = self.make_server(
			self.provider, "reconcile-in-flight", provider_resource_id="r5", status="Pending"
		)
		self._backdate(server.name, worker.RECONCILE_PENDING_GRACE_SECONDS + 60)
		with patch.object(worker, "enqueue_finish_provisioning", return_value=False):
			re_enqueued = worker.reconcile_pending_servers()
		self.assertNotIn(server.name, re_enqueued)

	def test_reconciler_is_registered_in_scheduler(self) -> None:
		# The safety net is only a safety net if it actually runs on a schedule.
		from atlas import hooks

		cron_jobs = [job for jobs in hooks.scheduler_events.get("cron", {}).values() for job in jobs]
		self.assertIn("atlas.atlas.providers.worker.reconcile_pending_servers", cron_jobs)
