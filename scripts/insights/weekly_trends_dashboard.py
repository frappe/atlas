from workbook import (
	COUNT,
	HOST,
	PERCENT,
	SECONDS,
	TITLES,
	WEEK,
	Layout,
	Workbook,
	daily_increase_sql,
	dimension,
	line_style,
	measure,
	trend,
)


def host_daily_sql(metric: str, aggregate: str = "avg") -> str:
	"""One cell per host and day: the day's average, or highest, value of one host gauge."""
	return f"""WITH {TITLES}
SELECT toDate(s.ts) AS day, {HOST.format(alias="s")} AS host, {aggregate}(s.value) AS value
FROM samples AS s LEFT JOIN titles AS t ON t.server = s.resource_id
WHERE s.metric = '{metric}' AND s.ts >= {WEEK}
GROUP BY day, host"""


def atlas_daily_last_sql(metric: str, label: str, divisor: int = 1) -> str:
	"""One row per day and label value: the value at Atlas's last push of the day, over divisor."""
	return f"""WITH last_pushes AS (
  SELECT toDate(ts) AS day, max(ts) AS last_ts
  FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric = '{metric}' AND ts >= {WEEK}
  GROUP BY day
)
SELECT p.day AS day, s.labels['{label}'] AS series, sum(s.value) / {divisor} AS value
FROM samples AS s INNER JOIN last_pushes AS p ON p.last_ts = s.ts
WHERE s.resource_id LIKE 'atlas-%' AND s.metric = '{metric}' AND s.ts >= {WEEK}
GROUP BY day, series"""


def daily_percentiles_sql(metric: str, where: str = "") -> str:
	"""One row per day and percentile of one observation metric."""
	return f"""SELECT toDate(ts) AS day, pair.1 AS series, pair.2 AS value
FROM (
  SELECT toDate(ts) AS ts, quantiles(0.75, 0.9, 0.95, 0.99)(value) AS q
  FROM samples
  WHERE metric = '{metric}' AND ts >= {WEEK} {where}
  GROUP BY ts
)
ARRAY JOIN [('p75', q[1]), ('p90', q[2]), ('p95', q[3]), ('p99', q[4])] AS pair"""


def heatmap(value_name: str, number_format: dict, maximum: float | None = 100) -> dict:
	"""One cell per host and day, colored by value."""
	config = {
		"x_column": dimension("day", "Date", granularity="day"),
		"y_column": dimension("host"),
		"value_column": measure(value_name, "value", "max"),
		"show_values": True,
		"palette": "sequential",
		"number_format": number_format,
	}
	if maximum is not None:
		config.update({"min": 0, "max": maximum})
	return config


def add_weekly_trends_dashboard(workbook: Workbook) -> None:
	"""The fleet over the last 7 days, one point per day, for a daily review."""
	folder = workbook.folder("Weekly Trends")
	daily = [
		folder.add_chart(
			"trend-host-cpu",
			"Host CPU Busy, last 7 days",
			host_daily_sql("system_cpu_usage_percent"),
			"Heatmap",
			heatmap("CPU %", PERCENT),
			"Average CPU busy share of each host per day.",
		),
		folder.add_chart(
			"trend-host-memory",
			"Host RAM Used, last 7 days",
			host_daily_sql("system_memory_usage_percent"),
			"Heatmap",
			heatmap("RAM %", PERCENT),
			"Average RAM use of each host per day.",
		),
		folder.add_chart(
			"trend-host-pool",
			"Storage Pool Used, last 7 days",
			host_daily_sql("system_zfs_pool_capacity_percent", "max"),
			"Heatmap",
			heatmap("Pool %", PERCENT),
			"Highest storage pool use of each host per day.",
		),
		folder.add_chart(
			"trend-vm-states",
			"VMs by Status, last 7 days",
			atlas_daily_last_sql("atlas_virtual_machines", "state"),
			"Line",
			trend("VMs", COUNT),
			"VMs in each status at the end of each day.",
		),
		folder.add_chart(
			"trend-vm-actions",
			"VM Actions per Day",
			f"""SELECT toDate(ts) AS day, labels['action'] AS series, count() AS value
FROM samples WHERE metric = 'metal_vm_action_duration_seconds' AND ts >= {WEEK} GROUP BY day, series""",
			"Line",
			trend("Actions", COUNT),
			"Completed VM actions on all hosts, by action.",
		),
		folder.add_chart(
			"trend-vm-failures",
			"Failed VM Operations per Day",
			daily_increase_sql(
				["metal_vm_operations_total"], "labels['phase']", "AND labels['result'] = 'failed'"
			),
			"Line",
			trend("Failures", COUNT),
			"Reconcile steps that failed on all hosts, by phase.",
		),
		folder.add_chart(
			"trend-migrations",
			"Migrations per Day",
			f"""SELECT toDate(ts) AS day, labels['result'] AS series, count() AS value
FROM samples WHERE metric = 'atlas_migration_duration_seconds' AND ts >= {WEEK} GROUP BY day, series""",
			"Line",
			trend("Migrations", COUNT),
			"Migrations that finished each day, by result.",
		),
		folder.add_chart(
			"trend-migration-duration",
			"Migration Duration per Day",
			daily_percentiles_sql("atlas_migration_duration_seconds", "AND labels['result'] = 'completed'"),
			"Line",
			trend("Seconds", SECONDS, stack=False),
			"Percentiles of the time from start to finish of completed migrations.",
		),
		folder.add_chart(
			"trend-images",
			"VM Images by Status, last 7 days",
			atlas_daily_last_sql("atlas_virtual_machine_images", "status"),
			"Line",
			trend("Images", COUNT),
			"VM images in each status at the end of each day.",
		),
		folder.add_chart(
			"trend-image-storage",
			"VM Image Storage, last 7 days",
			atlas_daily_last_sql("atlas_virtual_machine_image_stored_bytes", "status", divisor=1 << 30),
			"Line",
			trend("GiB", {"decimals": 1, "suffix": " GiB"}),
			"Stored size of image and kernel files at the end of each day, by image status.",
		),
		folder.add_chart(
			"trend-placement",
			"Placement Time per Day",
			daily_percentiles_sql("atlas_placement_duration_seconds"),
			"Line",
			trend("Seconds", {"decimals": 3, "suffix": " s"}, stack=False),
			"Percentiles of host selection time. The target is under 100 ms.",
		),
		folder.add_chart(
			"trend-call-errors",
			"API and Metal Call Errors per Day",
			daily_increase_sql(
				["atlas_api_requests_total", "atlas_metal_requests_total"],
				"if(metric = 'atlas_api_requests_total', concat('API ', labels['status']), concat('Metal ', labels['status']))",
				"AND resource_id LIKE 'atlas-%' AND ((metric = 'atlas_api_requests_total' AND labels['status'] LIKE '5%') OR (metric = 'atlas_metal_requests_total' AND labels['status'] NOT LIKE '2%'))",
			),
			"Line",
			trend("Errors", COUNT),
			"Atlas API responses with a 5xx status, and Atlas calls to Metal that failed, by status.",
		),
	]
	layout = Layout(workbook, "d-weekly-trends", "Atlas - Weekly Trends")
	sections = [
		("Hosts", daily[0:3]),
		("Virtual Machines", daily[3:6]),
		("Migrations", daily[6:8]),
		("Images", daily[8:10]),
		("Atlas Calls", daily[10:12]),
	]
	for heading, keys in sections:
		layout.heading(heading)
		for key in keys:
			layout.chart(key)
		layout.new_row()
