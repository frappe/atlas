"""Unit tests for the Central seam (spec/16-central.md).

Covers the three logic surfaces a host can't add anything to:
  - CentralClient request building (URL, auth header, error wrapping, unwrap).
  - upsert_central_sizes / upsert_central_images (insert / update / disable;
    bake_status resolution against Virtual Machine Image).
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

from atlas.atlas import central, central_report
from atlas.atlas.central import (
	CentralClient,
	CentralError,
	CentralImageInfo,
	CentralSizeInfo,
)


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

	def test_fetch_sizes_parses_rows(self) -> None:
		payload = {
			"message": {
				"sizes": [
					{
						"slug": "shared-1x",
						"title": "Shared 1x",
						"vcpus": 1,
						"cpu_max_cores": 0.0625,
						"memory_megabytes": 512,
						"disk_gigabytes": 10,
						"monthly_cost_usd": 4,
					}
				]
			}
		}
		with patch("atlas.atlas.central.requests.request") as request:
			request.return_value = _response(body=payload)
			sizes = self.client.fetch_sizes()
		self.assertEqual(len(sizes), 1)
		self.assertEqual(sizes[0].slug, "shared-1x")
		self.assertEqual(sizes[0].cpu_max_cores, 0.0625)

	def test_post_event_raises_on_error(self) -> None:
		with patch("atlas.atlas.central.requests.request") as request:
			request.return_value = _response(status_code=500, body={"exc": "boom"})
			with self.assertRaises(CentralError):
				self.client.post_event({"type": "vm.created"})


class TestCentralUpsert(IntegrationTestCase):
	def tearDown(self) -> None:
		for slug in ("shared-1x", "shared-2x", "old-size"):
			if frappe.db.exists("Central Size", slug):
				frappe.delete_doc("Central Size", slug, force=True, ignore_permissions=True)
		for name in ("bench-v15", "old-image"):
			if frappe.db.exists("Central Image", name):
				frappe.delete_doc("Central Image", name, force=True, ignore_permissions=True)

	def test_upsert_sizes_insert_update_disable(self) -> None:
		# Seed a stale row Central will no longer list -> should be disabled.
		frappe.get_doc({"doctype": "Central Size", "slug": "old-size", "enabled": 1, "vcpus": 1}).insert(
			ignore_permissions=True
		)

		sizes = (
			CentralSizeInfo("shared-1x", "Shared 1x", 1, 0.0625, 512, 10, 4),
			CentralSizeInfo("shared-2x", "Shared 2x", 1, 0.125, 1024, 20, 8),
		)
		first = central.upsert_central_sizes(sizes)
		self.assertEqual(first["inserted"], 2)
		self.assertEqual(first["disabled"], 1)
		self.assertEqual(frappe.db.get_value("Central Size", "old-size", "enabled"), 0)
		self.assertEqual(frappe.db.get_value("Central Size", "shared-1x", "memory_megabytes"), 512)

		# Re-run: same rows now update, not insert.
		second = central.upsert_central_sizes(sizes)
		self.assertEqual(second["inserted"], 0)
		self.assertEqual(second["updated"], 2)

	def test_upsert_images_bake_status(self) -> None:
		images = (CentralImageInfo("bench-v15", "Bench V15", "v15"),)
		# No matching VM Image yet -> Expected.
		central.upsert_central_images(images)
		self.assertEqual(frappe.db.get_value("Central Image", "bench-v15", "bake_status"), "Expected")
		self.assertFalse(frappe.db.get_value("Central Image", "bench-v15", "local_image"))


@contextlib.contextmanager
def _patched_emit():
	"""Patch out _emit's side effects so a handler test stays unit-pure: enabled,
	the MyISAM log-row insert (stubbed to return name cel-1), and enqueue. Yields
	the enqueue mock — the assertion surface for the tests."""
	with (
		patch.object(central_report, "_enabled", return_value=True),
		patch.object(central_report, "_write_log", return_value="cel-1"),
		patch.object(central_report.frappe, "enqueue") as enqueue,
	):
		yield enqueue


class TestCentralReport(IntegrationTestCase):
	_patched_emit = staticmethod(_patched_emit)

	def _vm(self, status="Running", before_status="Pending"):
		doc = SimpleNamespace(
			name="vm-1", title="vm-1", status=status, server="srv-1", doctype="Virtual Machine", tenant=None
		)
		doc.get = lambda key, default=None: getattr(doc, key, default)
		doc.get_doc_before_save = lambda: (
			SimpleNamespace(status=before_status) if before_status is not None else None
		)
		return doc

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
			central_report.deliver("cel-1", "vm.created", {"name": "vm-1"})
		settings.client.return_value.post_event.assert_called_once()
		stamp.assert_called_once_with("cel-1", status="ok", http_status=200)

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


class TestCentralReportSite(IntegrationTestCase):
	"""Site lifecycle events: created on insert, status_changed on a status flip,
	with the admin handoff (url + admin_password) only carried once Running."""

	def _site(self, status="Pending", before_status=None, tenant=None):
		doc = SimpleNamespace(
			name="acme.blr1.frappe.dev",
			status=status,
			subdomain="acme",
			tenant=tenant,
			doctype="Site",
		)
		doc.get = lambda key, default=None: getattr(doc, key, default)
		doc.get_password = lambda field: "atlas-baked"
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
		self.assertIsNone(payload["admin_password"])

	def test_running_event_carries_handoff(self) -> None:
		with _patched_emit() as enqueue:
			central_report.on_site_update(self._site(status="Running", before_status="Deploying"))
		payload = enqueue.call_args.kwargs["payload"]
		self.assertEqual(payload["url"], "https://acme.blr1.frappe.dev")
		self.assertEqual(payload["admin_password"], "atlas-baked")

	def test_no_status_change_skips(self) -> None:
		with (
			patch.object(central_report, "_enabled", return_value=True),
			patch.object(central_report.frappe, "enqueue") as enqueue,
		):
			central_report.on_site_update(self._site(status="Running", before_status="Running"))
		enqueue.assert_not_called()
