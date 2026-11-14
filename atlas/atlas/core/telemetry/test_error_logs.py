import json
from datetime import datetime, timedelta

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import now_datetime

from atlas.atlas.core.telemetry.error_logs import CURSOR_KEY, get_new_error_logs, to_log_line

TRACEBACK = (
	'Traceback (most recent call last):\n  File "app.py", line 1\nfrappe.exceptions.PermissionError: \n'
)


def error_log(title: str, metadata: dict) -> frappe._dict:
	return frappe._dict(
		name="error-1",
		creation=datetime(2026, 10, 14, 11, 15),
		method=title,
		error=TRACEBACK,
		fingerprint="6e8d5ae7",
		trace_id="request-1",
		metadata=json.dumps({"form_dict": {"token": "secret"}, **metadata}),
		reference_doctype=None,
		reference_name=None,
	)


class TestErrorLogLines(UnitTestCase):
	def test_a_failed_request_is_unhandled_and_names_its_exception(self) -> None:
		# Frappe titles an unhandled request error with the exception line, trailing space included.
		line = to_log_line(
			error_log("frappe.exceptions.PermissionError: ", {"type": "http_request", "path": "/api/v1/vms"})
		)

		self.assertEqual(line["attributes"]["unhandled"], "1")
		self.assertEqual(line["attributes"]["exception"], "frappe.exceptions.PermissionError")
		self.assertEqual(line["attributes"]["request_id"], "request-1")
		self.assertNotIn("secret", json.dumps(line))

	def test_an_error_that_atlas_logs_on_purpose_is_handled(self) -> None:
		line = to_log_line(
			error_log("Metal synchronization failed for Server s1", {"type": "background_job"})
		)

		self.assertEqual(line["attributes"]["unhandled"], "0")

	def test_a_failed_job_is_unhandled(self) -> None:
		method = "atlas.metal_server.usage.sync_server"
		line = to_log_line(error_log(method, {"type": "background_job", "job_name": method}))

		self.assertEqual(line["attributes"]["unhandled"], "1")
		self.assertEqual(line["attributes"]["path"], method)


class TestNewErrorLogs(IntegrationTestCase):
	def test_a_new_error_log_is_read_after_the_cursor(self) -> None:
		frappe.cache.delete_value(CURSOR_KEY)
		name = frappe.log_error("Atlas telemetry cursor test").name

		self.assertIn(name, [row.name for row in get_new_error_logs()])

	def test_an_old_iso_cursor_still_finds_new_error_logs(self) -> None:
		frappe.cache.set_value(CURSOR_KEY, (now_datetime() - timedelta(minutes=1)).isoformat())
		name = frappe.log_error("Atlas telemetry ISO cursor test").name

		self.assertIn(name, [row.name for row in get_new_error_logs()])
