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


class TestCheckNetworkdLiveness(IntegrationTestCase):
	"""The controller-side liveness BACKSTOP: flag (never reconfigure) any Active
	host whose atlas-networkd daemon is not healthy. Read-only by construction."""

	def setUp(self) -> None:
		from atlas.tests.fixtures import make_provider, make_server

		self.provider = make_provider("test-provider-liveness")
		self.make_server = make_server

	def test_healthy_hosts_are_not_flagged(self) -> None:
		server = self.make_server(
			self.provider, "liveness-healthy", provider_resource_id="lh1", status="Active"
		)
		with (
			patch.object(worker, "_probe_networkd_liveness", return_value=(True, "active")),
			patch.object(worker.frappe, "log_error") as log_error,
		):
			unhealthy = worker.check_networkd_liveness()
		self.assertNotIn(server.name, unhealthy)
		# A healthy host raises nothing operator-facing.
		for call in log_error.call_args_list:
			self.assertNotIn(server.name, str(call))

	def test_unhealthy_host_is_flagged_and_surfaced(self) -> None:
		server = self.make_server(
			self.provider, "liveness-unhealthy", provider_resource_id="lu1", status="Active"
		)

		def _probe(name: str) -> tuple[bool, str]:
			# Only this test's host is unhealthy; the shared DB may hold others.
			return (name != server.name, "failed" if name == server.name else "active")

		with (
			patch.object(worker, "_probe_networkd_liveness", side_effect=_probe),
			patch.object(worker.frappe, "log_error") as log_error,
		):
			unhealthy = worker.check_networkd_liveness()
		self.assertIn(server.name, unhealthy)
		# Surfaced as an Error Log the operator can see (the codebase's convention).
		self.assertTrue(any(server.name in str(c) for c in log_error.call_args_list))

	def test_unreachable_host_does_not_abort_the_sweep(self) -> None:
		# One host whose probe raises (SSH timeout, no ipv4) must be flagged but must
		# NOT prevent the other Active hosts from being probed.
		bad = self.make_server(
			self.provider, "liveness-unreachable", provider_resource_id="lx1", status="Active"
		)
		good = self.make_server(
			self.provider, "liveness-reachable", provider_resource_id="lx2", status="Active"
		)

		def _probe(name: str) -> tuple[bool, str]:
			if name == bad.name:
				raise RuntimeError("ssh timeout")
			return True, "active"

		with (
			patch.object(worker, "_probe_networkd_liveness", side_effect=_probe),
			patch.object(worker.frappe, "log_error"),
		):
			unhealthy = worker.check_networkd_liveness()
		self.assertIn(bad.name, unhealthy)
		self.assertNotIn(good.name, unhealthy)

	def test_fake_servers_are_skipped(self) -> None:
		# A Fake server has no host to SSH; it must never be probed.
		fake = self.make_server(
			self.provider, "liveness-fake", provider_resource_id="lf1", status="Active", provider_type="Fake"
		)
		with (
			patch.object(worker, "_probe_networkd_liveness") as probe,
			patch.object(worker.frappe, "log_error"),
		):
			worker.check_networkd_liveness()
		for call in probe.call_args_list:
			self.assertNotEqual(call.args[0], fake.name)

	def test_probe_is_read_only(self) -> None:
		# The probe must only OBSERVE (systemctl is-active / cat status.json) — never
		# push config or restart. Assert every SSH command it issues is read-only.
		server = self.make_server(
			self.provider, "liveness-probe-readonly", provider_resource_id="lp1", status="Active"
		)
		issued: list[str] = []

		def _fake_run_ssh(connection, key_path, remote_command, *params, **kwargs):
			issued.append(remote_command)
			# First call = the is-active/is-enabled probe: report healthy.
			return "active\nenabled\n", "", 0

		with (
			patch("atlas.atlas.ssh.connection_for_server", return_value=MagicMock()),
			patch("atlas.atlas.ssh.ssh_key_file") as key_file,
			patch("atlas.atlas.ssh.run_ssh", side_effect=_fake_run_ssh),
		):
			key_file.return_value.__enter__.return_value = "/tmp/key"
			healthy, detail = worker._probe_networkd_liveness(server.name)
		self.assertTrue(healthy)
		self.assertEqual(detail, "active")
		# No command may reconfigure the mesh — no syncconf, no restart, no seed push.
		joined = " ; ".join(issued).lower()
		for forbidden in ("syncconf", "restart", "systemctl start", "install ", "tee "):
			self.assertNotIn(forbidden, joined)

	def test_backstop_is_registered_in_scheduler(self) -> None:
		# The backstop only backstops if it actually runs on a schedule.
		from atlas import hooks

		cron_jobs = [job for jobs in hooks.scheduler_events.get("cron", {}).values() for job in jobs]
		self.assertIn("atlas.atlas.providers.worker.check_networkd_liveness", cron_jobs)
