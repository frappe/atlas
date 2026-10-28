from __future__ import annotations

import functools
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import frappe
import requests
from frappe import _

REQUEST_TIMEOUT_SECONDS = 45
MAXIMUM_EXECUTION_SECONDS = 30
MAXIMUM_ROWS = 500
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
WORKBOOK_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "insights" / "generate_workbook.py"
ALERT_QUERY_PREFIX = "q-alert-"
FRESHNESS_SQL = """
SELECT resource_id, max(ts) AS last_metric_at, dateDiff('second', max(ts), now()) AS metric_age_seconds
FROM samples WHERE ts > now() - INTERVAL 1 DAY GROUP BY resource_id ORDER BY resource_id
"""
LOG_FRESHNESS_SQL = """
SELECT resource_id, service, max(ts) AS last_log_at, dateDiff('second', max(ts), now()) AS log_age_seconds
FROM logs WHERE ts > now() - INTERVAL 1 DAY GROUP BY resource_id, service ORDER BY resource_id, service
"""

# A drop in a counter is a process restart, so the value after the drop is the increase.
COUNTER_INCREASE_SQL = """
SELECT resource_id, metric, labels, arraySum(arrayMap((change, value) -> if(change >= 0, change, value),
	arrayDifference(values), values)) AS increase, length(values) AS samples
FROM (
	SELECT resource_id, metric, labels, arrayMap(sample -> sample.2, arraySort(groupArray((ts, value)))) AS values
	FROM samples
	WHERE metric IN {metrics:Array(String)} AND ts > now() - INTERVAL {minutes:UInt32} MINUTE
		AND ({resource_id:String} = '' OR resource_id = {resource_id:String})
	GROUP BY resource_id, metric, labels
)
WHERE increase != 0 ORDER BY increase DESC
"""
LATEST_VALUES_SQL = """
SELECT metric, labels, argMax(value, ts) AS value, dateDiff('second', max(ts), now()) AS age_seconds
FROM samples
WHERE resource_id = {resource_id:String} AND metric IN {metrics:Array(String)} AND ts > now() - INTERVAL 15 MINUTE
GROUP BY metric, labels ORDER BY metric
"""
# Each distinct build label set is a version. Each distinct start or boot time is a restart or a reboot.
CHANGES_SQL = """
SELECT metric, labels, round(value) AS value, min(ts) AS first_seen, max(ts) AS last_seen
FROM samples
WHERE resource_id = {resource_id:String} AND ts > now() - INTERVAL {hours:UInt32} HOUR
	AND metric IN ('metal_build_info', 'system_kernel_info', 'metal_process_start_time_seconds', 'system_boot_time_seconds')
GROUP BY metric, labels, value ORDER BY first_seen
"""
RECONCILER_COUNTERS = (
	"metal_reconciler_passes_total", "metal_reconciler_failures_total", "metal_vm_operations_total",
	"metal_api_requests_total",
)  # fmt: skip
RECONCILER_GAUGES = (
	"metal_reconciler_last_pass_timestamp_seconds", "metal_vm_operations_in_progress",
	"metal_vm_oldest_pending_seconds", "metal_vm_pending", "metal_vm_failing", "metal_vm_unavailable",
)  # fmt: skip
STALE_LOOP_SECONDS = 300


class TelemetryQueryError(Exception):
	"""Datum refused or failed a query."""


@dataclass(frozen=True)
class TelemetryResult:
	columns: list[str]
	rows: list[list]
	is_truncated: bool

	def as_dict(self) -> dict:
		return {"columns": self.columns, "rows": self.rows, "is_truncated": self.is_truncated}


@dataclass(frozen=True)
class SavedQuery:
	key: str
	title: str
	folder: str
	sql: str


class TelemetryQuery:
	"""Read-only SQL on the regional Datum ClickHouse, through its HTTP interface.

	The ClickHouse user must be read-only (`readonly = 2 CONST`). Atlas also limits rows and time.
	"""

	def __init__(self) -> None:
		settings = frappe.get_cached_doc("Atlas Settings")
		if not (settings.telemetry_query_url and settings.telemetry_query_username):
			frappe.throw(_("Set the Telemetry Query URL and Username in Atlas Settings."))

		self.url = settings.telemetry_query_url
		self.database = settings.telemetry_query_database or "datum"
		self.username = settings.telemetry_query_username
		self.password = settings.get_password("telemetry_query_password", raise_exception=False) or ""

	def run(
		self, sql: str, parameters: dict[str, str] | None = None, row_limit: int = 100
	) -> TelemetryResult:
		"""Run one statement and return at most row_limit rows."""
		row_limit = max(1, min(row_limit, MAXIMUM_ROWS))
		query_parameters = {
			"database": self.database,
			"default_format": "JSONCompact",
			"max_execution_time": MAXIMUM_EXECUTION_SECONDS,
			"max_result_rows": row_limit + 1,
			"result_overflow_mode": "break",
			**{f"param_{name}": value for name, value in (parameters or {}).items()},
		}
		response = requests.post(
			self.url,
			params=query_parameters,
			data=sql.encode(),
			auth=(self.username, self.password),
			timeout=REQUEST_TIMEOUT_SECONDS,
		)
		if response.status_code != 200:
			raise TelemetryQueryError(response.text.strip()[:2000])

		body = response.json()
		rows = body.get("data", [])
		return TelemetryResult(
			columns=[column["name"] for column in body.get("meta", [])],
			rows=rows[:row_limit],
			is_truncated=len(rows) > row_limit,
		)

	def run_saved_query(
		self,
		key: str,
		filter_column: str | None = None,
		filter_value: str | None = None,
		row_limit: int = 100,
	) -> TelemetryResult:
		"""Run one Atlas workbook query, optionally narrowed to rows where a column equals a value."""
		sql = get_saved_query(key).sql
		if not filter_column:
			return self.run(sql, row_limit=row_limit)

		if not IDENTIFIER.match(filter_column):
			frappe.throw(_("{0} is not a valid column name.").format(filter_column))
		return self.run(
			f"SELECT * FROM ({sql}) WHERE toString(`{filter_column}`) = {{filter_value:String}}",
			parameters={"filter_value": filter_value or ""},
			row_limit=row_limit,
		)

	def get_failing_alert_checks(self) -> dict[str, list[dict]]:
		"""Run each Atlas alert query. Return the failing checks of each alert by title."""
		failing = {}
		for query in get_saved_queries():
			if query.key.startswith(ALERT_QUERY_PREFIX):
				result = self.run(query.sql, row_limit=MAXIMUM_ROWS)
				failing[query.title] = [dict(zip(result.columns, row, strict=True)) for row in result.rows]
		return failing

	def get_freshness(self) -> dict:
		"""Return the age of the newest metric and log line of each resource. Old data means blind telemetry."""
		return {
			"metrics": self.run(FRESHNESS_SQL, row_limit=MAXIMUM_ROWS).as_dict(),
			"logs": self.run(LOG_FRESHNESS_SQL, row_limit=MAXIMUM_ROWS).as_dict(),
		}

	def get_counter_increase(
		self, metrics: list[str], resource_id: str = "", minutes: int = 60
	) -> TelemetryResult:
		"""Return the increase of each counter series in the window, per resource, metric, and labels."""
		if not metrics or not all(
			IDENTIFIER.match(metric) and metric.endswith("_total") for metric in metrics
		):
			frappe.throw(_("Give only counter names. A counter name ends with _total."))
		return self.run(
			COUNTER_INCREASE_SQL,
			parameters={"metrics": _array(metrics), "resource_id": resource_id, "minutes": str(minutes)},
			row_limit=MAXIMUM_ROWS,
		)

	def get_reconciler_status(self, resource_id: str) -> dict:
		"""Return the metald loops of a host with the age of their last pass, and the counters of the last hour."""
		latest = self.run(
			LATEST_VALUES_SQL,
			parameters={"resource_id": resource_id, "metrics": _array(RECONCILER_GAUGES)},
			row_limit=MAXIMUM_ROWS,
		)
		rows = [dict(zip(latest.columns, row, strict=True)) for row in latest.rows]
		loops = {}
		for row in rows:
			if row["metric"] == "metal_reconciler_last_pass_timestamp_seconds":
				age = int(time.time() - row["value"])
				loops[row["labels"]["loop"]] = {
					"last_pass_age_seconds": age,
					"is_stale": age > STALE_LOOP_SECONDS,
				}
		return {
			"loops": loops,
			"gauges": [
				row for row in rows if row["metric"] != "metal_reconciler_last_pass_timestamp_seconds"
			],
			"increase_last_hour": self.get_counter_increase(list(RECONCILER_COUNTERS), resource_id).as_dict(),
		}

	def get_changes(self, resource_id: str, hours: int) -> TelemetryResult:
		"""Return the metald and kernel versions, metald start times, and boot times that a host reported."""
		return self.run(
			CHANGES_SQL, parameters={"resource_id": resource_id, "hours": str(hours)}, row_limit=MAXIMUM_ROWS
		)


def _array(names: list[str] | tuple[str, ...]) -> str:
	"""A ClickHouse Array(String) parameter of identifiers, which need no escape."""
	return "[" + ", ".join(f"'{name}'" for name in names) + "]"


@functools.cache
def get_saved_queries() -> tuple[SavedQuery, ...]:
	"""Return the queries of the Atlas Insights workbook. Its scripts are the one source of this SQL."""
	output = subprocess.run(
		[sys.executable, WORKBOOK_SCRIPT.name],
		cwd=WORKBOOK_SCRIPT.parent,
		capture_output=True,
		check=True,
		text=True,
		timeout=60,
	).stdout
	return tuple(
		SavedQuery(
			key=key, title=query["title"], folder=query["folder"], sql=query["operations"][0]["raw_sql"]
		)
		for key, query in json.loads(output)["queries"].items()
	)


def get_saved_query(key: str) -> SavedQuery:
	for query in get_saved_queries():
		if query.key == key:
			return query
	frappe.throw(_("No saved query has the key {0}.").format(key))
