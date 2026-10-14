from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import frappe
import requests
from frappe.utils import get_system_timezone, now_datetime
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from atlas.atlas.core.telemetry.datum import (
	get_atlas_resource_id,
	get_datum_url,
	issue_datum_token,
	send_log_lines,
)

CURSOR_KEY = "atlas:telemetry:error_log_cursor"

# One request a minute stays under the Datum limit of 12 requests a minute.
MAXIMUM_LINES = 1_000
TRACEBACK_TAIL_CHARACTERS = 1_024

# The last traceback line names the exception: "frappe.exceptions.PermissionError: ...".
EXCEPTION_LINE = re.compile(r"^([A-Za-z_][\w.]*)(?::|$)")

ERROR_LOG_FIELDS = [
	"name",
	"creation",
	"method",
	"error",
	"fingerprint",
	"trace_id",
	"metadata",
	"reference_doctype",
	"reference_name",
]


def publish_error_logs() -> None:
	"""Send new Error Logs to Datum, with one publisher at a time."""
	url = get_datum_url()
	if not url:
		return

	try:
		with filelock("atlas:telemetry:error-logs", timeout=0):
			send_new_error_logs(url)
	except LockTimeoutError:
		return


def send_new_error_logs(url: str) -> None:
	"""Send the new Error Logs and move the cursor past them when Datum accepts them."""
	rows = get_new_error_logs()
	if not rows:
		return

	try:
		send_log_lines(url, issue_datum_token(get_atlas_resource_id()), [to_log_line(row) for row in rows])
	except requests.RequestException as error:
		frappe.logger("atlas.telemetry").warning(f"Datum error log push failed: {error}")
		return
	frappe.cache.set_value(CURSOR_KEY, rows[-1].creation.isoformat())


def get_new_error_logs() -> list[frappe._dict]:
	"""Read after the cursor, leaving a partial timestamp group for the next batch."""
	cursor = frappe.cache.get_value(CURSOR_KEY) or (now_datetime() - timedelta(minutes=5)).isoformat()
	rows = frappe.get_all(
		"Error Log",
		filters={"creation": [">", cursor]},
		fields=ERROR_LOG_FIELDS,
		order_by="creation asc",
		limit=MAXIMUM_LINES,
	)
	if len(rows) == MAXIMUM_LINES:
		last = rows[-1].creation
		rows = [row for row in rows if row.creation < last] or rows
	return rows


def to_log_line(row: frappe._dict) -> dict:
	"""Return one Datum log line for an Error Log. The form data stays out."""
	metadata = frappe.parse_json(row.metadata or "{}") or {}
	traceback = (row.error or "").strip()
	last_line = traceback.splitlines()[-1] if traceback else ""
	match = EXCEPTION_LINE.match(last_line)
	title = (row.method or last_line).strip()
	created = row.creation.replace(tzinfo=ZoneInfo(get_system_timezone())).astimezone(UTC)

	attributes = {
		"error_log": row.name,
		"fingerprint": row.fingerprint or "",
		"request_id": row.trace_id or "",
		"exception": match.group(1) if match else "",
		"unhandled": "1" if is_unhandled(title, last_line, metadata) else "0",
		"kind": metadata.get("type") or "",
		"path": metadata.get("path") or metadata.get("job_name") or "",
		"reference_doctype": row.reference_doctype or "",
		"reference_name": row.reference_name or "",
		"traceback": traceback[-TRACEBACK_TAIL_CHARACTERS:],
	}
	return {
		"ts": created.isoformat(),
		"product": "atlas",
		"service": "atlas",
		"level": "error",
		"source": "error_log",
		"message": title,
		"attributes": {name: value for name, value in attributes.items() if value},
	}


def is_unhandled(title: str, last_line: str, metadata: dict) -> bool:
	"""Identify unhandled failures from Atlas and Frappe error titles."""
	if title.startswith("Unhandled "):
		return True
	if metadata.get("type") == "background_job":
		return title == metadata.get("job_name")
	return metadata.get("type") == "http_request" and bool(last_line) and last_line.endswith(title)
