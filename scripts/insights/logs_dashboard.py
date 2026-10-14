from workbook import HOST, LOOKBACK, TITLES, Layout, Workbook, dimension, measure, table, time_series

LINES = measure("Lines", "count", "count", "Integer")


def logs_sql(where: str, columns: str = "") -> str:
	"""One row per log line, with the VM and request ID of the line."""
	return f"""WITH {TITLES}
SELECT l.ts AS ts, {HOST.format(alias="l")} AS host, l.service AS service, l.level AS level,
  l.message AS message, l.attributes['virtual_machine_id'] AS vm, l.attributes['request_id'] AS request_id {columns}
FROM logs AS l LEFT JOIN titles AS t ON t.server = l.resource_id
WHERE l.ts > now() - {LOOKBACK} {where}"""


def log_table(columns: list[str], values: list[dict], limit: int = 200) -> dict:
	"""A table of log lines, newest first."""
	rows = [dimension("ts", "Datetime", granularity="second")] + [dimension(column) for column in columns]
	return table(rows, values, order="ts", limit=limit)


def add_logs_dashboard(workbook: Workbook) -> None:
	"""Find what happened to one VM, request, or host, from Metal logs."""
	folder = workbook.folder("Logs")

	volume = folder.add_chart(
		"log-volume",
		"Log Volume",
		logs_sql("", ", l.level AS series"),
		"Line",
		time_series(LINES, granularity="hour", split_values=6, stack=True),
	)
	trace = folder.add_chart(
		"trace",
		"Trace",
		logs_sql(
			"AND (l.attributes['request_id'] != '' OR l.attributes['virtual_machine_id'] != '')",
			", l.attributes['phase'] AS phase, l.attributes['path'] AS path, l.attributes['status'] AS status",
		),
		"Table",
		log_table(["host", "message", "phase", "path", "status", "vm", "request_id"], [LINES], limit=300),
		"Set the VM or Request filter to follow one change from the API call to the applied state.",
	)
	actions = folder.add_chart(
		"action-events",
		"VM Actions",
		logs_sql(
			"AND l.message = 'virtual machine action completed'",
			", l.attributes['action'] AS action, toInt64OrZero(l.attributes['generation']) AS generation, "
			"toInt64OrZero(l.attributes['duration']) / 1e9 AS duration_seconds",
		),
		"Table",
		log_table(
			["host", "vm", "action", "request_id"],
			[
				measure("Seconds", "duration_seconds", "max"),
				measure("Generation", "generation", "max", "Integer"),
			],
		),
	)
	problems = folder.add_chart(
		"problems",
		"Warnings and Errors",
		logs_sql("AND l.level IN ('warn', 'warning', 'error', 'critical', 'alert', 'emergency')"),
		"Table",
		log_table(["host", "service", "level", "vm", "message"], [LINES]),
	)
	migrations = folder.add_chart(
		"migration-events",
		"Migration Steps",
		logs_sql(
			"AND l.message = 'migration state changed'",
			", l.attributes['migration_id'] AS migration, l.attributes['from'] AS from_state, "
			"l.attributes['to'] AS to_state, l.attributes['error'] AS error",
		),
		"Table",
		log_table(["host", "vm", "migration", "from_state", "to_state", "error"], [LINES]),
	)
	charts = [volume, trace, actions, problems, migrations]

	layout = Layout(workbook, "d-logs", "Atlas - Logs")
	layout.filter(
		"Period",
		"Date",
		workbook.filter_links(charts, "ts"),
		default_operator="within",
		default_value={"span": "last 1 day (include current)"},
	)
	layout.filter("Host", "String", workbook.filter_links(charts, "host"), default_operator="in")
	layout.filter("VM", "String", workbook.filter_links(charts, "vm"), default_operator="in")
	layout.filter("Request", "String", workbook.filter_links(charts, "request_id"), default_operator="=")
	layout.new_row()
	layout.chart(volume, 20, 12)
	for chart in (trace, actions, problems, migrations):
		layout.chart(chart, 20, 28)
