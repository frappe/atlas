from workbook import (
	COUNT,
	ERRORS,
	HOST,
	HOST_ERRORS,
	TITLES,
	WEEK,
	Layout,
	Workbook,
	daily_increase_sql,
	dimension,
	measure,
	table,
	trend,
)

FOUR_WEEKS = "toStartOfWeek(today(), 1) - 21"

ERROR_KIND = "if(attributes['unhandled'] = '1', 'Unhandled', 'Handled')"


def error_counts_sql(period: str, start: str) -> str:
	"""Atlas Error Logs per day or week since start, handled and unhandled."""
	return f"""SELECT {period}(ts) AS day, {ERROR_KIND} AS series, count() AS value
FROM logs
WHERE {ERRORS} AND ts >= {start}
GROUP BY day, series"""


API_ERROR_RATE = f"""SELECT day, 'per 1,000 API requests' AS series,
  sumIf(value, series LIKE '5%') / nullIf(sum(value), 0) * 1000 AS value
FROM ({daily_increase_sql(["atlas_api_requests_total"], "labels['status']", "AND resource_id LIKE 'atlas-%'")})
GROUP BY day"""


ERRORS_BY_SIDE = f"""SELECT toDate(ts) AS day, if(service = 'atlas' AND source = 'error_log', 'Atlas', 'Hosts') AS series,
  count() AS value
FROM logs
WHERE ts >= {WEEK} AND (({ERRORS}) OR ({HOST_ERRORS}))
GROUP BY day, series"""


def host_errors_sql(series: str) -> str:
	"""Host error lines per day, one series per host or per service."""
	return f"""WITH {TITLES}
SELECT toDate(l.ts) AS day, {series} AS series, count() AS value
FROM logs AS l LEFT JOIN titles AS t ON t.server = l.resource_id
WHERE l.ts >= {WEEK} AND l.{HOST_ERRORS.replace(" AND level", " AND l.level")}
GROUP BY day, series"""


TOP_HOST_ERRORS = f"""SELECT service, left(message, 120) AS message, any(left(attributes['error'], 160)) AS example,
  uniqExact(resource_id) AS hosts, count() AS occurrences, min(ts) AS first_seen, max(ts) AS last_seen
FROM logs
WHERE ts >= {WEEK} AND {HOST_ERRORS}
GROUP BY service, message"""


def top_errors_sql(start: str) -> str:
	"""One row per error fingerprint: how often it happened, and when it started and last happened."""
	return f"""SELECT if(attributes['fingerprint'] = '', message, attributes['fingerprint']) AS fingerprint,
  any(left(message, 120)) AS title, any(attributes['exception']) AS exception,
  if(max(attributes['unhandled']) = '1', 'yes', 'no') AS unhandled, count() AS occurrences,
  min(ts) AS first_seen, max(ts) AS last_seen, uniqExact(toDate(ts)) AS days
FROM logs
WHERE {ERRORS} AND ts >= {start}
GROUP BY fingerprint"""


TOP_ERRORS_DAILY = f"""WITH top AS (
  SELECT if(attributes['fingerprint'] = '', message, attributes['fingerprint']) AS fingerprint, any(left(message, 60)) AS title
  FROM logs
  WHERE {ERRORS} AND ts >= {WEEK}
  GROUP BY fingerprint
  ORDER BY count() DESC
  LIMIT 5
)
SELECT toDate(l.ts) AS day, if(t.title = '', 'Other', t.title) AS series, count() AS value
FROM logs AS l LEFT JOIN top AS t ON t.fingerprint = if(l.attributes['fingerprint'] = '', l.message, l.attributes['fingerprint'])
WHERE l.source = 'error_log' AND l.service = 'atlas' AND l.ts >= {WEEK}
GROUP BY day, series
ORDER BY day DESC, value DESC"""


def add_errors_dashboard(workbook: Workbook) -> None:
	"""Atlas Error Logs: volume, rate, and the most frequent errors, by day and by week."""
	folder = workbook.folder("Errors")
	columns = [
		measure("Occurrences", "occurrences", "sum", "Integer"),
		measure("Days", "days", "max", "Integer"),
		measure("First Seen", "first_seen", "min", "Datetime"),
		measure("Last Seen", "last_seen", "max", "Datetime"),
	]
	rows = [dimension("title"), dimension("exception"), dimension("unhandled")]
	errors = [
		folder.add_chart(
			"errors-daily",
			"Errors per Day",
			error_counts_sql("toDate", WEEK),
			"Line",
			trend("Errors", COUNT),
			"Atlas Error Logs per day. Unhandled errors are failed requests and jobs that no Atlas code caught.",
		),
		folder.add_chart(
			"errors-weekly",
			"Errors per Week",
			error_counts_sql("toMonday", FOUR_WEEKS),
			"Line",
			trend("Errors", COUNT, granularity="week"),
			"Atlas Error Logs per week, for the last 4 weeks.",
		),
		folder.add_chart(
			"errors-rate",
			"API Error Rate per Day",
			API_ERROR_RATE,
			"Line",
			trend("Errors", {"decimals": 1}, stack=False),
			"Tenant API responses with a 5xx status per 1,000 requests.",
		),
		folder.add_chart(
			"errors-top-daily",
			"Top 5 Errors per Day",
			TOP_ERRORS_DAILY,
			"Table",
			table(
				[dimension("day", "Date", granularity="day"), dimension("series", name="error")],
				[measure("Occurrences", "value", "sum", "Integer")],
				limit=100,
			),
			"Each day's count of the 5 most frequent errors of the last 7 days. Other errors are grouped.",
		),
		folder.add_chart(
			"errors-top-week",
			"Top Errors, last 7 days",
			top_errors_sql(WEEK),
			"Table",
			table(rows, columns, order="Occurrences", limit=20),
			"Errors grouped by fingerprint, most frequent first.",
		),
		folder.add_chart(
			"errors-top-month",
			"Top Errors, last 4 weeks",
			top_errors_sql(FOUR_WEEKS),
			"Table",
			table(rows, columns, order="Occurrences", limit=20),
			"Errors grouped by fingerprint, most frequent first.",
		),
	]
	daily, weekly, rate, top_daily, top_week, top_month = errors
	overview = folder.add_chart(
		"errors-by-side",
		"Errors per Day",
		ERRORS_BY_SIDE,
		"Line",
		trend("Errors", COUNT),
		"Atlas errors are Error Logs. Host errors are lines at level error or worse from a Metal host.",
	)
	host_by_host = folder.add_chart(
		"errors-host-by-host",
		"Host Errors per Day, by Host",
		host_errors_sql(HOST.format(alias="l")),
		"Line",
		trend("Errors", COUNT),
		"Error lines from each Metal host.",
	)
	host_by_service = folder.add_chart(
		"errors-host-by-service",
		"Host Errors per Day, by Service",
		host_errors_sql("l.service"),
		"Line",
		trend("Errors", COUNT),
		"Error lines from metald, the kernel, and each systemd unit, on all hosts.",
	)
	top_host = folder.add_chart(
		"errors-host-top",
		"Top Host Errors, last 7 days",
		TOP_HOST_ERRORS,
		"Table",
		table(
			[dimension("service"), dimension("message"), dimension("example")],
			[
				measure("Hosts", "hosts", "max", "Integer"),
				measure("Occurrences", "occurrences", "sum", "Integer"),
				measure("First Seen", "first_seen", "min", "Datetime"),
				measure("Last Seen", "last_seen", "max", "Datetime"),
			],
			order="Occurrences",
			limit=50,
		),
		"Host error lines grouped by service and message. Hosts is how many hosts logged the error.",
	)

	layout = Layout(workbook, "d-errors", "Atlas - Errors")
	layout.heading("Atlas and Hosts")
	layout.chart(overview, 20, 14)
	layout.heading("Atlas")
	layout.chart(rate, 20, 14)
	layout.chart(daily)
	layout.chart(weekly)
	for key in (top_daily, top_week, top_month):
		layout.chart(key, 20, 28)
	layout.heading("Hosts")
	layout.chart(host_by_host)
	layout.chart(host_by_service)
	layout.chart(top_host, 20, 28)
