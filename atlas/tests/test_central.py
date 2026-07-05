"""Unit tests for the Central seam (spec/16-central.md).

Covers the logic surfaces a host can't add anything to:
  - CentralClient request building (URL, auth header, error wrapping, unwrap).
  - central_report gating, payload shape, and enqueue_after_commit.

No live Central call — requests is monkeypatched.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from atlas.atlas import central_report
from atlas.atlas.central import CentralClient, CentralError
from atlas.tests import fixtures


def _response(status_code=200, body=None, content=True):
	resp = SimpleNamespace()
	resp.status_code = status_code
	resp.text = json.dumps(body) if body is not None else ""
	resp.content = b"x" if content else b""
	resp.json = lambda: body
	return resp


class TestCentralClient(IntegrationTestCase):
	def setUp(self) -> None:
		self.client = CentralClient("https://central.example/", "ak", "secret")

	def test_request_builds_url_and_auth_header(self) -> None:
		with patch("atlas.atlas.central.requests.request") as request:
			request.return_value = _response(body={"message": {"label": "Central"}})
			self.client.ping()
		args, kwargs = request.call_args
		self.assertEqual(args[0], "GET")
		self.assertEqual(args[1], "https://central.example/api/method/central.api.atlas.ping")
		self.assertEqual(kwargs["headers"]["Authorization"], "token ak:secret")

	def test_ping_ok(self) -> None:
		with patch("atlas.atlas.central.requests.request") as request:
			request.return_value = _response(body={"message": {"label": "Prod Central"}})
			result = self.client.ping()
		self.assertTrue(result.ok)
		self.assertEqual(result.label, "Prod Central")

	def test_ping_never_raises_on_http_error(self) -> None:
		with patch("atlas.atlas.central.requests.request") as request:
			request.return_value = _response(status_code=401, body={"exc": "auth"})
			result = self.client.ping()
		self.assertFalse(result.ok)
		self.assertIn("401", result.error)

	def test_ping_never_raises_on_transport_error(self) -> None:
		import requests as requests_lib

		with patch("atlas.atlas.central.requests.request", side_effect=requests_lib.ConnectionError("down")):
			result = self.client.ping()
		self.assertFalse(result.ok)
		self.assertIn("down", result.error)

	def test_post_event_raises_on_error(self) -> None:
		with patch("atlas.atlas.central.requests.request") as request:
			request.return_value = _response(status_code=500, body={"exc": "boom"})
			with self.assertRaises(CentralError):
				self.client.post_event({"type": "vm.created"})


@contextlib.contextmanager
def _patched_emit():
	"""Patch out _emit's side effects so a handler test stays unit-pure: enabled,
	the MyISAM log-row insert (stubbed to return name cel-1), and enqueue. Yields
	the enqueue mock — the assertion surface for the tests."""
	with (
		patch.object(central_report, "_enabled", return_value=True),
		patch.object(central_report, "_write_log", return_value="cel-1"),
		patch.object(central_report, "_mark_log_after_transaction"),
		patch.object(central_report.frappe, "enqueue") as enqueue,
	):
		yield enqueue


class TestCentralReport(IntegrationTestCase):
	_patched_emit = staticmethod(_patched_emit)

	def _vm(self, status="Running", before_status="Pending", resizing=False):
		# A plain VM stand-in — no Pilot backs "vm-1", so _vm_payload's pilot_for_vm
		# lookup returns None and the bench fields (gateway_url/login_url/expiry) are all
		# None. The bench handoff is exercised via _pilot_vm_payload below.
		doc = SimpleNamespace(
			name="vm-1",
			title="vm-1",
			status=status,
			server="srv-1",
			doctype="Virtual Machine",
			tenant=None,
		)
		doc.get = lambda key, default=None: getattr(doc, key, default)
		doc.flags = frappe._dict(resizing=resizing)
		doc.get_doc_before_save = lambda: (
			SimpleNamespace(status=before_status) if before_status is not None else None
		)
		return doc

	def test_vm_payload_echoes_pilot_credential_id(self) -> None:
		vm = self._vm()
		vm.pilot_credential_id = "pcred-abc"
		self.assertEqual(central_report._vm_payload(vm)["pilot_credential_id"], "pcred-abc")

	def test_vm_payload_pilot_credential_id_none_when_unset(self) -> None:
		self.assertIsNone(central_report._vm_payload(self._vm())["pilot_credential_id"])

	def test_plain_vm_payload_has_no_bench_fields(self) -> None:
		# A VM with no owning Pilot carries no front door.
		payload = central_report._vm_payload(self._vm())
		self.assertIsNone(payload["gateway_url"])
		self.assertIsNone(payload["login_url"])
		self.assertIsNone(payload["login_url_expires_at"])

	def test_pilot_vm_payload_expiry_is_json_serializable(self) -> None:
		# _merge_bench_fields renders the Pilot's expiry via _iso so requests' stdlib
		# json.dumps (no default=str) can POST it. _pilot_vm_payload reads plain VM facts
		# through the link, so stand in a real VM the pilot points at.
		import datetime

		server = fixtures.make_server(
			fixtures.make_provider("central-vm-provider"),
			"central-vm-server",
			ipv6_address="2001:db8:c::1",
			ipv6_prefix="2001:db8:c::/64",
			ipv6_virtual_machine_range="2001:db8:c::/124",
		)
		vm = fixtures.make_virtual_machine(server, fixtures.make_image("central-vm-image"), title="pilot-vm")
		# gateway_url is derived from the front door's name (its fqdn) — for a real Pilot
		# name == <subdomain>.<region domain>, so stand that in as the name here.
		pilot = SimpleNamespace(
			name="acme.blr1.frappe.dev",
			virtual_machine=vm.name,
			tenant=None,
			status="Running",
			login_url_expires_at=datetime.datetime(2026, 7, 4, 10, 19, 56),
		)
		pilot.get = lambda key, default=None: getattr(pilot, key, default)
		payload = central_report._pilot_vm_payload(pilot)
		self.assertEqual(payload["login_url_expires_at"], "2026-07-04 10:19:56")
		self.assertEqual(payload["gateway_url"], "https://acme.blr1.frappe.dev")
		json.dumps(payload)  # requests uses stdlib json.dumps with no default

	def test_disabled_emits_nothing(self) -> None:
		with (
			patch.object(central_report, "_enabled", return_value=False),
			patch.object(central_report.frappe, "enqueue") as enqueue,
		):
			central_report.on_vm_update(self._vm())
		enqueue.assert_not_called()

	def test_status_change_enqueues_after_commit(self) -> None:
		with self._patched_emit() as enqueue:
			central_report.on_vm_update(self._vm(status="Running", before_status="Pending"))
		enqueue.assert_called_once()
		args, kwargs = enqueue.call_args
		self.assertEqual(args[0], "atlas.atlas.central_report.deliver")
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["event_type"], "vm.status_changed")
		self.assertEqual(kwargs["payload"]["name"], "vm-1")
		self.assertEqual(kwargs["payload"]["status"], "Running")
		# The deliver job is handed the audit row to stamp.
		self.assertEqual(kwargs["log_name"], "cel-1")

	def test_write_log_row_snapshots_reference_and_starts_pending(self) -> None:
		"""_write_log builds the audit row snapshotting the source doctype/name (as
		Data, not a Link, so it survives the source's deletion) and starts it at
		pending — independent of delivery. Patches only the single get_doc call it
		makes, so no Frappe internals are affected."""
		with patch.object(central_report.frappe, "get_doc") as get_doc:
			get_doc.return_value.insert.return_value.name = "cel-1"
			name = central_report._write_log("vm.status_changed", {"name": "vm-1"}, self._vm())
		self.assertEqual(name, "cel-1")
		row = get_doc.call_args[0][0]
		self.assertEqual(row["doctype"], "Central Event Log")
		self.assertEqual(row["event_type"], "vm.status_changed")
		self.assertEqual(row["status"], "pending")
		self.assertEqual(row["attempts"], 0)
		self.assertEqual(row["reference_doctype"], "Virtual Machine")
		self.assertEqual(row["reference_name"], "vm-1")
		self.assertEqual(json.loads(row["payload"]), {"name": "vm-1"})

	def test_no_status_change_skips(self) -> None:
		with (
			patch.object(central_report, "_enabled", return_value=True),
			patch.object(central_report.frappe, "enqueue") as enqueue,
		):
			central_report.on_vm_update(self._vm(status="Running", before_status="Running"))
		enqueue.assert_not_called()

	def test_resize_emits_resized_without_status_change(self) -> None:
		# A resize keeps the VM Stopped (no status flip), so the flags.resizing
		# breadcrumb is what makes us emit vm.resized carrying the new shape.
		with self._patched_emit() as enqueue:
			central_report.on_vm_update(self._vm(status="Stopped", before_status="Stopped", resizing=True))
		enqueue.assert_called_once()
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["event_type"], "vm.resized")
		self.assertEqual(kwargs["payload"]["name"], "vm-1")

	def test_status_change_wins_over_resizing(self) -> None:
		# A resize that also flips status reports the status change (the mirror upsert
		# is identical either way); we never double-emit.
		with self._patched_emit() as enqueue:
			central_report.on_vm_update(self._vm(status="Stopped", before_status="Running", resizing=True))
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["event_type"], "vm.status_changed")

	def test_after_insert_emits_created(self) -> None:
		with self._patched_emit() as enqueue:
			central_report.on_vm_after_insert(self._vm(before_status=None))
		self.assertEqual(enqueue.call_args.kwargs["event_type"], "vm.created")

	def test_deliver_records_error_and_does_not_raise(self) -> None:
		settings = MagicMock()
		settings.enabled = 1
		settings.api_key = "svc_key"
		settings.client.return_value.post_event.side_effect = CentralError("central down", 503)
		with (
			patch.object(central_report.frappe, "get_single", return_value=settings),
			patch.object(central_report, "_stamp") as stamp,
			patch.object(central_report.frappe, "log_error"),
		):
			central_report.deliver("cel-1", "vm.created", {"name": "vm-1"})
		# The single's breadcrumb still records the error...
		settings.db_set.assert_called()
		recorded = settings.db_set.call_args[0]
		self.assertEqual(recorded[0], "status")
		self.assertIn("error", recorded[1])
		# ...and the audit row is stamped error with the HTTP status from Central.
		stamp.assert_called_once()
		self.assertEqual(stamp.call_args[0][0], "cel-1")
		self.assertEqual(stamp.call_args.kwargs["status"], "error")
		self.assertEqual(stamp.call_args.kwargs["http_status"], 503)

	def test_deliver_stamps_ok_on_success(self) -> None:
		settings = MagicMock()
		settings.enabled = 1
		settings.api_key = "svc_key"
		with (
			patch.object(central_report.frappe, "get_single", return_value=settings),
			patch.object(central_report, "_stamp") as stamp,
		):
			central_report.deliver("cel-1", "vm.created", {"name": "vm-1"}, occurred_at="2026-07-06 00:23:05")
		settings.client.return_value.post_event.assert_called_once()
		event = settings.client.return_value.post_event.call_args[0][0]
		self.assertEqual(event["occurred_at"], "2026-07-06 00:23:05")
		stamp.assert_called_once_with("cel-1", status="ok", http_status=200)

	def test_retry_pending_replays_queued_events_in_order(self) -> None:
		rows = [
			frappe._dict(
				name="cel-1",
				event_type="vm.status_changed",
				payload=json.dumps({"name": "vm-1", "status": "Stopped"}),
				occurred_at="2026-07-06 00:23:05",
			),
			frappe._dict(
				name="cel-2",
				event_type="vm.status_changed",
				payload=json.dumps({"name": "vm-2", "status": "Running"}),
				occurred_at="2026-07-06 00:24:00",
			),
		]
		with (
			patch.object(central_report, "_delivery_ready", return_value=True),
			patch.object(central_report.frappe, "get_all", return_value=rows) as get_all,
			patch.object(central_report, "deliver") as deliver,
		):
			count = central_report.retry_pending(limit=1000)
		self.assertEqual(count, 2)
		self.assertEqual(get_all.call_args.kwargs["filters"], {"status": ["in", ["queued"]]})
		self.assertEqual(get_all.call_args.kwargs["limit"], central_report.MAX_PENDING_RETRY_BATCH)
		deliver.assert_any_call(
			"cel-1",
			"vm.status_changed",
			{"name": "vm-1", "status": "Stopped"},
			occurred_at="2026-07-06 00:23:05",
		)
		deliver.assert_any_call(
			"cel-2",
			"vm.status_changed",
			{"name": "vm-2", "status": "Running"},
			occurred_at="2026-07-06 00:24:00",
		)

	def test_retry_pending_can_replay_legacy_pending_when_explicit(self) -> None:
		with (
			patch.object(central_report, "_delivery_ready", return_value=True),
			patch.object(central_report.frappe, "get_all", return_value=[]) as get_all,
			patch.object(central_report, "deliver"),
		):
			count = central_report.retry_pending(limit=2, include_legacy_pending=True)
		self.assertEqual(count, 0)
		self.assertEqual(get_all.call_args.kwargs["filters"], {"status": ["in", ["queued", "pending"]]})

	def test_retry_pending_returns_before_query_when_delivery_not_ready(self) -> None:
		with (
			patch.object(central_report, "_delivery_ready", return_value=False),
			patch.object(central_report.frappe, "get_all") as get_all,
			patch.object(central_report, "deliver") as deliver,
		):
			count = central_report.retry_pending()
		self.assertEqual(count, 0)
		get_all.assert_not_called()
		deliver.assert_not_called()

	def test_retry_pending_returns_before_delivery_when_no_rows(self) -> None:
		with (
			patch.object(central_report, "_delivery_ready", return_value=True),
			patch.object(central_report.frappe, "get_all", return_value=[]),
			patch.object(central_report, "deliver") as deliver,
		):
			count = central_report.retry_pending()
		self.assertEqual(count, 0)
		deliver.assert_not_called()

	def test_deliver_skips_when_unregistered(self) -> None:
		settings = MagicMock()
		settings.enabled = 1
		settings.api_key = None  # enabled but no service-user creds yet
		with (
			patch.object(central_report.frappe, "get_single", return_value=settings),
			patch.object(central_report, "_stamp") as stamp,
		):
			central_report.deliver("cel-1", "vm.created", {"name": "vm-1"})
		settings.client.assert_not_called()
		stamp.assert_called_once_with("cel-1", status="skipped")

	def test_log_row_survives_rollback(self) -> None:
		"""The load-bearing claim: a Central Event Log row written by _write_log
		survives a rollback of the surrounding transaction, because the table is
		MyISAM (non-transactional). This is the whole reason the doctype exists and
		it can only be asserted against the real table, so it lives here."""
		name = central_report._write_log("vm.status_changed", {"name": "vm-rb"}, self._vm())
		# Roll back the request transaction, as a failed VM save would.
		frappe.db.rollback()
		# An InnoDB row would be gone now; the MyISAM audit row is still here.
		self.assertTrue(frappe.db.exists("Central Event Log", name))
		row = frappe.get_doc("Central Event Log", name)
		self.assertEqual(row.status, "pending")
		self.assertEqual(row.event_type, "vm.status_changed")
		self.assertEqual(row.reference_name, "vm-1")
		row.delete()
		frappe.db.commit()

	def test_rollback_marks_event_log_audit_only(self) -> None:
		name = central_report._write_log("vm.status_changed", {"name": "vm-rb"}, self._vm())
		central_report._mark_log_after_transaction(name)
		frappe.db.rollback()
		try:
			self.assertEqual(frappe.db.get_value("Central Event Log", name, "status"), "rolled_back")
		finally:
			frappe.delete_doc("Central Event Log", name, force=True, ignore_permissions=True)
			frappe.db.commit()


class TestCentralReportSite(IntegrationTestCase):
	"""Site lifecycle events: created on insert, status_changed on a status flip,
	with the admin handoff (url + login_url) only carried once Running."""

	def _site(self, status="Pending", before_status=None, tenant=None):
		doc = SimpleNamespace(
			name="acme.blr1.frappe.dev",
			status=status,
			subdomain="acme",
			tenant=tenant,
			doctype="Site",
			login_url="https://acme.blr1.frappe.dev/app?sid=abc123",
			login_url_expires_at="2026-07-02 12:00:00",
		)
		doc.get = lambda key, default=None: getattr(doc, key, default)
		doc.get_doc_before_save = lambda: (
			SimpleNamespace(status=before_status) if before_status is not None else None
		)
		return doc

	def test_after_insert_emits_created(self) -> None:
		with _patched_emit() as enqueue:
			central_report.on_site_after_insert(self._site())
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["event_type"], "site.created")
		self.assertEqual(kwargs["payload"]["name"], "acme.blr1.frappe.dev")
		self.assertEqual(kwargs["payload"]["subdomain"], "acme")

	def test_status_change_emits_and_pending_hides_handoff(self) -> None:
		with _patched_emit() as enqueue:
			central_report.on_site_update(self._site(status="Provisioning", before_status="Pending"))
		payload = enqueue.call_args.kwargs["payload"]
		self.assertEqual(enqueue.call_args.kwargs["event_type"], "site.status_changed")
		self.assertEqual(payload["status"], "Provisioning")
		self.assertIsNone(payload["url"])
		self.assertIsNone(payload["login_url"])
		self.assertIsNone(payload["login_url_expires_at"])

	def test_running_event_carries_handoff(self) -> None:
		with _patched_emit() as enqueue:
			central_report.on_site_update(self._site(status="Running", before_status="Deploying"))
		payload = enqueue.call_args.kwargs["payload"]
		self.assertEqual(payload["url"], "https://acme.blr1.frappe.dev")
		self.assertEqual(payload["login_url"], "https://acme.blr1.frappe.dev/app?sid=abc123")
		self.assertEqual(payload["login_url_expires_at"], "2026-07-02 12:00:00")

	def test_no_status_change_skips(self) -> None:
		with (
			patch.object(central_report, "_enabled", return_value=True),
			patch.object(central_report.frappe, "enqueue") as enqueue,
		):
			central_report.on_site_update(self._site(status="Running", before_status="Running"))
		enqueue.assert_not_called()

	def test_report_site_status_emits_on_the_db_set_transition(self) -> None:
		"""The bug this closes: auto_provision drives every transition through
		_set_status → db_set, which fires on_change, not on_update — so the doc_event
		never pushes and the mirror is stuck at the initial Pending. report_site_status
		is the explicit emit that carries each transition (Provisioning..Running)."""
		with _patched_emit() as enqueue:
			central_report.report_site_status(self._site(status="Provisioning"))
		enqueue.assert_called_once()
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["event_type"], "site.status_changed")
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["payload"]["status"], "Provisioning")
		# Pre-Running: no handoff to carry yet.
		self.assertIsNone(kwargs["payload"]["login_url"])

	def test_report_site_status_running_carries_handoff(self) -> None:
		with _patched_emit() as enqueue:
			central_report.report_site_status(self._site(status="Running"))
		payload = enqueue.call_args.kwargs["payload"]
		self.assertEqual(payload["status"], "Running")
		self.assertEqual(payload["url"], "https://acme.blr1.frappe.dev")
		self.assertEqual(payload["login_url"], "https://acme.blr1.frappe.dev/app?sid=abc123")

	def test_report_site_status_no_op_when_disabled(self) -> None:
		with (
			patch.object(central_report, "_enabled", return_value=False),
			patch.object(central_report.frappe, "enqueue") as enqueue,
		):
			central_report.report_site_status(self._site(status="Running"))
		enqueue.assert_not_called()


class TestCentralReportPilot(IntegrationTestCase):
	"""Pilot lifecycle events. A Pilot reports AS its backing VM, so the payload is
	VM-shaped with the front-door fields folded on. The Running flip in auto_provision
	is a db_set (skips validation), which never fires on_update — so the push that
	carries the login handoff rides an explicit report_pilot_status() call instead of
	the doc_event. These tests pin that the explicit emit still delivers the handoff."""

	def _pilot_with_vm(self, status="Running", *, login_url="https://acme.blr1.frappe.dev/app?sid=abc"):
		server = fixtures.make_server(
			fixtures.make_provider("pilot-evt-provider"),
			"pilot-evt-server",
			ipv6_address="2001:db8:e::1",
			ipv6_prefix="2001:db8:e::/64",
			ipv6_virtual_machine_range="2001:db8:e::/124",
		)
		vm = fixtures.make_virtual_machine(server, fixtures.make_image("pilot-evt-image"), title="pilot-vm")
		pilot = SimpleNamespace(
			virtual_machine=vm.name,
			tenant=None,
			status=status,
			gateway_url="https://acme.blr1.frappe.dev",
			login_url=login_url,
			login_url_expires_at="2026-07-04 10:19:56",
			doctype="Pilot",
			name="acme.blr1.frappe.dev",
		)
		pilot.get = lambda key, default=None: getattr(pilot, key, default)
		return pilot

	def test_report_pilot_status_emits_handoff_on_the_db_set_running_flip(self) -> None:
		"""The bug this closes: auto_provision flips Running via db_set (no on_update),
		so report_pilot_status must be what pushes the freshly-minted login_url — the
		mirror would otherwise only learn it on the next 10-min reconcile."""
		pilot = self._pilot_with_vm(status="Running")
		with _patched_emit() as enqueue:
			central_report.report_pilot_status(pilot)
		enqueue.assert_called_once()
		kwargs = enqueue.call_args.kwargs
		self.assertEqual(kwargs["event_type"], "vm.status_changed")
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["payload"]["status"], "Running")
		self.assertEqual(kwargs["payload"]["login_url"], "https://acme.blr1.frappe.dev/app?sid=abc")
		self.assertEqual(kwargs["payload"]["gateway_url"], "https://acme.blr1.frappe.dev")

	def test_report_pilot_status_no_op_when_disabled(self) -> None:
		pilot = self._pilot_with_vm()
		with (
			patch.object(central_report, "_enabled", return_value=False),
			patch.object(central_report.frappe, "enqueue") as enqueue,
		):
			central_report.report_pilot_status(pilot)
		enqueue.assert_not_called()

	def test_report_pilot_status_no_op_without_backing_vm(self) -> None:
		pilot = self._pilot_with_vm()
		pilot.virtual_machine = None
		with self._patched_emit_no_vm() as enqueue:
			central_report.report_pilot_status(pilot)
		enqueue.assert_not_called()

	@staticmethod
	@contextlib.contextmanager
	def _patched_emit_no_vm():
		# Enabled, but no VM to read through — assert we bail before touching _emit.
		with (
			patch.object(central_report, "_enabled", return_value=True),
			patch.object(central_report.frappe, "enqueue") as enqueue,
		):
			yield enqueue

	def test_pre_running_pilot_hides_the_handoff(self) -> None:
		"""Before Running the mint hasn't happened — _merge_bench_fields blanks login_url
		even if a stale value sits on the row."""
		pilot = self._pilot_with_vm(status="Pending", login_url="https://leftover/app?sid=x")
		with _patched_emit() as enqueue:
			central_report.report_pilot_status(pilot)
		payload = enqueue.call_args.kwargs["payload"]
		self.assertEqual(payload["status"], "Pending")
		self.assertIsNone(payload["login_url"])


class TestFrontDoorResolvesSite(IntegrationTestCase):
	"""Regression: a `create_site` backing VM is owned by a SITE, never a Pilot, so the
	VM-shaped Asset payload (push `_vm_payload` + pull `tenant_vms`) must read the login
	handoff through the Site — otherwise the Central Asset "Open" is dead (no login_url).
	Before front_door_for_vm this resolved Pilot-only and left the Asset login-less."""

	ROOT_DOMAIN = "blr1.frappe.dev"
	REGION = "blr1"

	def setUp(self) -> None:
		frappe.db.set_single_value("Atlas Settings", "region", self.REGION)
		if not frappe.db.exists("Root Domain", self.ROOT_DOMAIN):
			frappe.get_doc(
				{
					"doctype": "Root Domain",
					"domain": self.ROOT_DOMAIN,
					"region": self.REGION,
					"is_active": 1,
					"dns_provider_type": "Route53",
					"tls_provider_type": "Let's Encrypt",
				}
			).insert(ignore_permissions=True)
		frappe.db.set_value("Root Domain", self.ROOT_DOMAIN, "is_active", 1)

	def _running_site_backed_vm(self, subdomain: str = "acme"):
		"""A tenant-tagged, Fake-backed VM owned by a Running Site with a minted
		login_url — the exact shape a completed create_site leaves behind."""
		from atlas.atlas.doctype.tenant.tenant import ensure_tenant

		tenant = ensure_tenant("fd-team", "fd@example.test")
		server = fixtures.make_server(
			fixtures.make_provider("fd-site-provider", provider_type="Fake"),
			"fd-site-server",
			ipv6_address="2001:db8:f::1",
			ipv6_prefix="2001:db8:f::/64",
			ipv6_virtual_machine_range="2001:db8:f::/124",
		)
		vm = fixtures.make_virtual_machine(
			server, fixtures.make_image("fd-site-image"), title=subdomain, tenant=tenant
		)
		site = frappe.get_doc(
			{"doctype": "Site", "subdomain": subdomain, "virtual_machine": vm.name, "tenant": tenant}
		).insert(ignore_permissions=True)
		self.login_url = f"https://{site.name}/app?sid=site-open"
		site.db_set("login_url", self.login_url)
		site.db_set("login_url_expires_at", "2026-07-05 12:00:00")
		site.db_set("status", "Running")
		return frappe.get_doc("Virtual Machine", vm.name), site

	def test_push_vm_payload_reads_login_through_the_site(self) -> None:
		vm, site = self._running_site_backed_vm("push")
		payload = central_report._vm_payload(vm)
		self.assertEqual(payload["gateway_url"], f"https://{site.name}")
		self.assertEqual(payload["login_url"], self.login_url)
		self.assertEqual(payload["login_url_expires_at"], "2026-07-05 12:00:00")

	def test_pull_tenant_vms_reads_login_through_the_site(self) -> None:
		"""The reconcile-pull must match the push — resolve the Site's handoff too, so a
		lost status_changed event doesn't leave the mirror login-less on the next sync."""
		from atlas.atlas.api import inventory

		vm, site = self._running_site_backed_vm("pull")
		row = next((r for r in inventory.tenant_vms() if r["name"] == vm.name), None)
		self.assertIsNotNone(row, "site-backed VM not returned by tenant_vms")
		self.assertEqual(row["gateway_url"], f"https://{site.name}")
		self.assertEqual(row["login_url"], self.login_url)

	def test_asset_prefers_pilot_console_when_one_backs_the_vm(self) -> None:
		"""A COMPLETED create_site backs its VM with BOTH a Site (customer site) and an
		attached Pilot (admin console). The Central Asset "Open" resolves the PILOT (a
		bench admin JWT), not the customer site — front_door_for_vm prefers Pilot. This is
		the bug fix: the Asset gateway/login point at the console, per spec/14-self-serve.md."""
		vm, site = self._running_site_backed_vm("both")
		# Attach a Running admin-mode Pilot to the SAME VM, as Site.auto_provision does.
		pilot = frappe.get_doc({"doctype": "Pilot", "subdomain": "both-pilot", "tenant": site.tenant})
		pilot.flags.attach_vm = vm.name
		pilot.insert(ignore_permissions=True)
		pilot_login = f"https://both-pilot.{self.ROOT_DOMAIN}/app?sid=admin-jwt"
		pilot.db_set("login_url", pilot_login)
		pilot.db_set("login_url_expires_at", "2026-07-05 12:00:00")
		pilot.db_set("status", "Running")
		payload = central_report._vm_payload(vm)
		# The Asset now opens the PILOT console FQDN + admin login, not the site's.
		self.assertEqual(payload["gateway_url"], f"https://both-pilot.{self.ROOT_DOMAIN}")
		self.assertEqual(payload["login_url"], pilot_login)
		self.assertNotEqual(payload["login_url"], self.login_url)
