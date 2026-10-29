from __future__ import annotations

from typing import Annotated

from frappe.flow import ToolContext

from atlas.operations.investigator.telemetry import TelemetryQuery, get_saved_queries
from atlas.operations.investigator.tools.decorators import read_tool
from atlas.operations.investigator.tools.inventory import get_host_name


@read_tool(background=True)
def list_failing_alert_checks(*, ctx: ToolContext) -> dict:
	"""Run the Atlas alert queries (P1, P2 Hosts, P2 Platform) and return each failing check. Start here."""
	return TelemetryQuery().get_failing_alert_checks()


@read_tool()
def list_saved_queries(
	search: Annotated[
		str | None, "Words to match in the title or folder, such as 'memory' or 'Host'."
	] = None,
	*,
	ctx: ToolContext,
) -> list[dict]:
	"""List the tested queries of the Atlas dashboards. Prefer them to your own SQL."""
	words = (search or "").lower().split()
	return [
		{"key": query.key, "title": query.title, "folder": query.folder}
		for query in get_saved_queries()
		if all(word in f"{query.title} {query.folder}".lower() for word in words)
	]


@read_tool(background=True)
def run_saved_query(
	key: Annotated[str, "Query key from list_saved_queries."],
	filter_column: Annotated[
		str | None, "Keep only rows where this result column equals filter_value."
	] = None,
	filter_value: Annotated[str | None, "Value for filter_column, such as a host title."] = None,
	row_limit: Annotated[int, "Rows to return, at most 500."] = 100,
	*,
	ctx: ToolContext,
) -> dict:
	"""Run one saved dashboard query on Datum."""
	return TelemetryQuery().run_saved_query(key, filter_column, filter_value, row_limit).as_dict()


@read_tool(background=True)
def query_telemetry(
	sql: Annotated[str, "One ClickHouse SELECT statement. Always limit the time range with ts."],
	row_limit: Annotated[int, "Rows to return, at most 500."] = 100,
	*,
	ctx: ToolContext,
) -> dict:
	"""Run your own read-only ClickHouse SQL on the Datum metrics and logs.

	Tables:
	- samples(ts DateTime64, metric String, resource_id String, labels Map(String, String), value Float64)
	- logs(ts DateTime64, resource_id String, product String, service String, level String, source String, message String, attributes Map(String, String))

	A host resource_id is its Metal Server name. Service nodes use their node name. Atlas uses atlas-<region>.
	"""
	return TelemetryQuery().run(sql, row_limit=row_limit).as_dict()


@read_tool(background=True)
def get_telemetry_freshness(*, ctx: ToolContext) -> dict:
	"""Return the age of the newest metric and log line of each resource. Old data means that Datum cannot see that resource, so read it over SSH."""
	return TelemetryQuery().get_freshness()


@read_tool(background=True)
def get_counter_increase(
	metrics: Annotated[list[str], "Counter names. Each ends with _total."],
	host: Annotated[str | None, "Metal Server name or title. Leave empty for all resources."] = None,
	minutes: Annotated[int, "Window, in minutes."] = 60,
	*,
	ctx: ToolContext,
) -> dict:
	"""Return how much each counter grew in the window, per resource, metric, and labels. Only rows that grew.
	Use it instead of your own SQL for a _total metric: it handles restarts and never mixes two metrics."""
	resource_id = get_host_name(host) if host else ""
	return TelemetryQuery().get_counter_increase(metrics, resource_id, minutes).as_dict()


@read_tool(background=True)
def get_reconciler_status(host: Annotated[str, "Metal Server name or title."], *, ctx: ToolContext) -> dict:
	"""Return each metald loop of a host with the age of its last pass, the pending and failing VM gauges,
	and the growth of passes, failures, VM operations, and API requests in the last hour."""
	return TelemetryQuery().get_reconciler_status(get_host_name(host))
