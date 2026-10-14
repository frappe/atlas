from platform_services_dashboard import STATUS_CHART as SERVICE_STATUS_CHART
from platform_services_dashboard import STATUS_READINGS
from region_tables import ACTIVE_MIGRATIONS, FLEET_HOSTS, HARDWARE, RECENT_PROBLEMS, REGION_HEALTH, STRESS
from slo_dashboard import SLA_CHART
from workbook import (
	COUNT,
	HOST,
	HOST_ERRORS,
	LOOKBACK,
	PERCENT,
	TITLES,
	Layout,
	Workbook,
	dimension,
	measure,
	numbers,
	table,
	time_series,
)

# One row per complete minute for the fleet. Each host pushes twice a minute and
# Atlas once, so the current minute is left out until every push has landed.
FLEET_TREND = f"""WITH
host_minutes AS (
  SELECT toStartOfMinute(ts) AS minute, resource_id,
    maxIf(value, metric = 'metal_vm_count' AND labels['state'] = 'running') AS running_vms,
    maxIfOrNull(value, metric = 'metal_vm_pending') AS host_pending,
    maxIf(value, metric = 'metal_vm_failing') AS failing,
    maxIf(value, metric = 'system_zfs_pool_capacity_percent') AS pool_used
  FROM samples
  WHERE metric IN ('metal_vm_count', 'metal_vm_pending', 'metal_vm_failing', 'system_zfs_pool_capacity_percent')
    AND ts > now() - {LOOKBACK}
  GROUP BY minute, resource_id
),
hosts AS (
  SELECT minute, countIf(host_pending IS NOT NULL) AS hosts_reporting, sum(running_vms) AS running_vms, sum(host_pending) AS pending,
    sum(failing) AS failing, max(pool_used) AS worst_pool_used
  FROM host_minutes
  GROUP BY minute
),
log_minutes AS (
  SELECT toStartOfMinute(ts) AS minute,
    countIf(service = 'atlas' AND source = 'error_log' AND attributes['unhandled'] = '1') AS unhandled_errors,
    countIf({HOST_ERRORS}) AS host_errors
  FROM logs
  WHERE ts > now() - {LOOKBACK} AND ((service = 'atlas' AND source = 'error_log') OR ({HOST_ERRORS}))
  GROUP BY minute
),
atlas AS (
  SELECT toStartOfMinute(ts) AS minute,
    maxIf(value, metric = 'atlas_error_logs_last_minute') AS errors,
    sumIf(value, metric = 'atlas_job_queue_length') / greatest(uniqExactIf(ts, metric = 'atlas_job_queue_length'), 1) AS queued_jobs,
    (1 - sumIf(value, metric = 'atlas_host_cpu_millicores' AND labels['kind'] = 'available')
      / nullIf(sumIf(value, metric = 'atlas_host_cpu_millicores' AND labels['kind'] = 'total'), 0)) * 100 AS cpu_allocated,
    (1 - sumIf(value, metric = 'atlas_host_memory_bytes' AND labels['kind'] = 'available')
      / nullIf(sumIf(value, metric = 'atlas_host_memory_bytes' AND labels['kind'] = 'total'), 0)) * 100 AS memory_allocated,
    (1 - sumIf(value, metric = 'atlas_host_storage_bytes' AND labels['kind'] = 'available')
      / nullIf(sumIf(value, metric = 'atlas_host_storage_bytes' AND labels['kind'] = 'total'), 0)) * 100 AS storage_allocated,
    {
	", ".join(
		f"""sumIf(value, metric = 'atlas_public_ip_{kind}' AND labels['version'] = '{version}'{status})
      / greatest(uniqExactIf(ts, metric = 'atlas_public_ip_{kind}'), 1) AS ipv{version}_{name}"""
		for version in (4,)
		for name, kind, status in (
			("total", "capacity", ""),
			("attached", "allocations", " AND labels['status'] != 'Available'"),
		)
	)
}
  FROM samples
  WHERE resource_id LIKE 'atlas-%' AND ts > now() - {LOOKBACK}
    AND metric IN ('atlas_error_logs_last_minute', 'atlas_job_failed', 'atlas_job_queue_length',
      'atlas_host_cpu_millicores', 'atlas_host_memory_bytes', 'atlas_host_storage_bytes',
      'atlas_public_ip_capacity', 'atlas_public_ip_allocations')
  GROUP BY minute
),
-- Failed jobs and failed Metal calls are counts that grow; a minute reads its growth.
growth AS (
  SELECT toStartOfMinute(ts) AS minute,
    sumIf(greatest(increase, 0), metric = 'atlas_job_failed') AS failed_jobs,
    sumIf(greatest(increase, 0), metric = 'atlas_metal_requests_total') AS metal_call_errors,
    sumIf(greatest(increase, 0), metric = 'atlas_api_requests_total') AS api_server_errors
  FROM (
    SELECT ts, metric, value - lagInFrame(value, 1, value) OVER w AS increase
    FROM samples
    WHERE resource_id LIKE 'atlas-%' AND ts > now() - {LOOKBACK}
      AND (metric = 'atlas_job_failed' OR (metric = 'atlas_metal_requests_total' AND labels['status'] NOT LIKE '2%')
        OR (metric = 'atlas_api_requests_total' AND labels['status'] LIKE '5%'))
    WINDOW w AS (PARTITION BY metric, labels ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
  )
  GROUP BY minute
)
SELECT h.minute AS ts, h.hosts_reporting AS hosts_reporting, h.running_vms AS running_vms, h.pending AS pending,
  h.failing AS failing, a.errors AS errors, coalesce(g.failed_jobs, 0) AS failed_jobs, a.queued_jobs AS queued_jobs,
  coalesce(g.metal_call_errors, 0) AS metal_call_errors, a.cpu_allocated AS cpu_allocated,
  a.memory_allocated AS memory_allocated, a.storage_allocated AS storage_allocated,
  coalesce(l.unhandled_errors, 0) AS unhandled_errors, coalesce(l.host_errors, 0) AS host_errors,
  coalesce(g.api_server_errors, 0) AS api_server_errors, h.worst_pool_used AS worst_pool_used,
  a.ipv4_total AS ipv4_total, a.ipv4_attached AS ipv4_attached, a.ipv4_total - a.ipv4_attached AS ipv4_available,
FROM hosts AS h
LEFT JOIN atlas AS a ON a.minute = h.minute
LEFT JOIN growth AS g ON g.minute = h.minute
LEFT JOIN log_minutes AS l ON l.minute = h.minute
WHERE h.minute < toStartOfMinute(now())"""

# Name, column, and number format of each fleet card, in reading order.
FLEET_READINGS = [
	("Hosts Reporting", "hosts_reporting", COUNT),
	("Running VMs", "running_vms", COUNT),
	("Pending Changes", "pending", COUNT),
	("Failing VMs", "failing", COUNT),
	("Queued Jobs", "queued_jobs", COUNT),
	("CPU Allocated", "cpu_allocated", PERCENT),
	("Memory Allocated", "memory_allocated", PERCENT),
	("Storage Allocated", "storage_allocated", PERCENT),
	("Worst Pool Used", "worst_pool_used", PERCENT),
	("IPv4 Total", "ipv4_total", COUNT),
	("IPv4 In Use", "ipv4_attached", COUNT),
	("IPv4 Free", "ipv4_available", COUNT),
]
# Errors read per hour: a minute is too short to show a trend.
HOURLY_READINGS = [
	("Atlas Errors/hour", "errors"),
	("Failed Jobs/hour", "failed_jobs"),
	("Metal Call Errors/hour", "metal_call_errors"),
	("Unhandled Errors/hour", "unhandled_errors"),
	("Host Errors/hour", "host_errors"),
	("API 5xx/hour", "api_server_errors"),
]
ERROR_TREND = f"""SELECT toStartOfHour(ts) AS ts, {", ".join(f"sum({column}) AS {column}" for _, column in HOURLY_READINGS)}
FROM ({FLEET_TREND})
WHERE ts < toStartOfHour(now())
GROUP BY ts"""

ACTION_NAMES = ["create", "start", "stop", "restart", "resize", "sleep", "update", "terminate"]
ACTION_TREND = f"""SELECT toStartOfHour(ts) AS ts,
  {", ".join(f"if(countIf(labels['action'] = '{action}') > 0, quantileIf(0.95)(value, labels['action'] = '{action}'), NULL) AS {action}" for action in ACTION_NAMES)}
FROM samples
WHERE metric = 'metal_vm_action_duration_seconds' AND ts > now() - {LOOKBACK}
GROUP BY ts"""


def observation_sql(metric: str, columns: str) -> str:
	"""One row per observed event, such as one migration."""
	return f"""WITH {TITLES}
SELECT s.ts AS ts, {HOST.format(alias="s")} AS host, {columns}, s.value AS value
FROM samples AS s LEFT JOIN titles AS t ON t.server = s.resource_id
WHERE s.metric = '{metric}' AND s.ts > now() - {LOOKBACK}"""


def add_region_dashboard(workbook: Workbook) -> None:
	"""Region health, the SLA and services, the fleet cards, the hosts worst first, then VM actions."""
	folder = workbook.folder("Region")

	fleet = folder.add_chart(
		"fleet-trend",
		"Fleet",
		FLEET_TREND,
		"Number",
		{
			**numbers(
				[measure(name, column, "avg") for name, column, _ in FLEET_READINGS],
				trend_grain="minute",
			),
			"number_formats": {name: number_format for name, _, number_format in FLEET_READINGS},
		},
		"The last complete minute of the fleet, with its trend over the period.",
	)
	errors = folder.add_chart(
		"error-trend",
		"Errors per Hour",
		ERROR_TREND,
		"Number",
		numbers(
			[measure(name, column, "sum") for name, column in HOURLY_READINGS],
			trend_grain="hour",
		),
		"The last complete hour, with its trend over the period.",
	)
	reading_charts = {name: fleet for name, *_ in FLEET_READINGS} | {
		name: errors for name, *_ in HOURLY_READINGS
	}

	hosts = workbook.query("q-fleet-hosts", "Fleet Hosts", FLEET_HOSTS, folder.title)
	hosts_with_issues = workbook.chart(
		"c-hosts-with-issues",
		"Hosts With Issues",
		hosts,
		"Number",
		numbers([measure("Hosts With Issues", "has_issues", "sum", "Integer")]),
		folder.title,
	)
	hosts_table = workbook.chart(
		"c-fleet-hosts",
		"Hosts",
		hosts,
		"Table",
		table(
			[dimension("host"), dimension("version")],
			[
				measure("Issues", "issues", "max", "Integer"),
				measure("Last Push s", "last_push_seconds", "max", "Integer"),
				measure("CPU %", "cpu", "max"),
				measure("RAM %", "memory", "max"),
				measure("Pool %", "pool", "max"),
				measure("CPU Allocated %", "cpu_allocated", "max"),
				measure("RAM Allocated %", "memory_allocated", "max"),
				measure("Disk Allocated %", "storage_allocated", "max"),
				measure("VMs", "total_vms", "max", "Integer"),
				measure("Running VMs", "running", "max", "Integer"),
				measure("Pending", "pending", "max", "Integer"),
				measure("Failing", "failing", "max", "Integer"),
				measure("Failed Units", "failed_units", "max", "Integer"),
				measure("Unhealthy Disks", "unhealthy_disks", "max", "Integer"),
			],
			order="Issues",
			scale="Issues",
		),
		folder.title,
		"Issues counts the problem kinds of a host: no push for 2 minutes, a failed unit, an unhealthy disk, a pool that is not online, or a degraded RAID array.",
	)
	health = folder.add_chart(
		"region-health",
		"Region Health",
		REGION_HEALTH,
		"Table",
		table(
			[dimension("status"), dimension("check"), dimension("healthy")],
			[measure("Now", "value", "max")],
			limit=50,
			status_colors={"ALERT": "red", "OK": "green"},
		),
		"Every check is OK when the region is healthy. Alerts sort first.",
	)
	problems = folder.add_chart(
		"recent-problems",
		"Recent Problems, last day",
		RECENT_PROBLEMS,
		"Table",
		table(
			[dimension("place"), dimension("kind"), dimension("problem"), dimension("detail")],
			[
				measure("Occurrences", "occurrences", "sum", "Integer"),
				measure("First Seen", "first_seen", "min", "Datetime"),
				measure("Last Seen", "last_seen", "max", "Datetime"),
			],
			order="Last Seen",
		),
		"Unhandled Atlas errors, host error lines, and failed migrations, one row for each problem and place.",
	)
	hardware = folder.add_chart(
		"hardware",
		"Hardware",
		HARDWARE,
		"Table",
		table(
			[dimension("host")],
			[
				measure("Problems", "problems", "max", "Integer"),
				measure("Unhealthy Disks", "unhealthy_disks", "max", "Integer"),
				measure("Degraded RAID", "degraded_arrays", "max", "Integer"),
				measure("ZFS Data Errors", "zfs_data_errors", "max", "Integer"),
				measure("ZFS Device Errors", "zfs_device_errors", "max", "Integer"),
				measure("ECC Corrected 24h", "ecc_corrected", "max", "Integer"),
				measure("ECC Uncorrected 24h", "ecc_uncorrected", "max", "Integer"),
				measure("CPU °C", "cpu_temperature", "max"),
				measure("Memory °C", "memory_temperature", "max"),
				measure("Throttles 24h", "thermal_throttles", "max", "Integer"),
				measure("Links Down", "links_down", "max", "Integer"),
				measure("NIC Errors 24h", "nic_errors", "max", "Integer"),
				measure("Failed Units", "failed_units", "max", "Integer"),
			],
			order="Problems",
			scale="Problems",
		),
		"Only hosts with a hardware fault. A rising ECC corrected count warns of a failing DIMM; CPU above 85 °C counts as a problem.",
	)
	stress = folder.add_chart(
		"stress",
		"Hosts Under Stress",
		STRESS,
		"Table",
		table(
			[dimension("host")],
			[
				measure("Stress Signals", "stress_signals", "max", "Integer"),
				measure("CPU Busy %", "cpu_busy", "max"),
				measure("Load per Core", "load_per_core", "max"),
				measure("CPU Pressure %", "cpu_pressure", "max"),
				measure("Memory Pressure %", "memory_pressure", "max"),
				measure("IO Pressure %", "io_pressure", "max"),
				measure("RAM Used %", "ram_used", "max"),
				measure("CPU Allocated %", "cpu_allocated", "max"),
				measure("RAM Allocated %", "memory_allocated", "max"),
			],
			order="Stress Signals",
			scale="Stress Signals",
		),
		"Only hosts with a stress signal in the last 15 minutes. Pressure is the share of time some tasks waited for CPU, memory, or IO. A signal is CPU pressure over 10%, "
		"memory pressure over 5%, IO pressure over 10%, RAM used over 90%, load over 1.5 per core, or allocation over 100%.",
	)
	active_migrations = folder.add_chart(
		"active-migrations",
		"Migrations in Progress",
		ACTIVE_MIGRATIONS,
		"Table",
		table(
			[
				dimension("migration"),
				dimension("vm"),
				dimension("destination"),
				dimension("step"),
				dimension("last_error"),
			],
			[measure("Started", "started", "min", "Datetime"), measure("Age Minutes", "age_minutes", "max")],
			order="Age Minutes",
		),
		"Migrations whose newest step is not completed or aborted, oldest first.",
	)

	actions = folder.add_chart(
		"action-trend",
		"VM Action p95",
		ACTION_TREND,
		"Number",
		{
			**numbers(
				[measure(action.title(), action, "max") for action in ACTION_NAMES],
				trend_grain="hour",
				is_latest=False,
			),
			"number_formats": {action.title(): {"decimals": 2, "suffix": " s"} for action in ACTION_NAMES},
		},
		"The slowest hourly 95th percentile in the period, from the Metal API write to the applied VM state, with its trend.",
	)
	migrations = folder.add_chart(
		"migrations",
		"Migrations",
		observation_sql("atlas_migration_duration_seconds", "s.labels['result'] AS result")
		+ " UNION ALL "
		+ observation_sql("metal_migration_downtime_seconds", "'guest downtime' AS result"),
		"Table",
		table(
			[dimension("result")],
			[
				measure("Count", "count", "count", "Integer"),
				measure("Avg s", "value", "avg"),
				measure("Max s", "value", "max"),
			],
		),
		"Migrations by the result Atlas recorded, and guest downtime from the source stop to the destination start.",
	)
	placement = folder.add_chart(
		"placement",
		"Placement Time",
		observation_sql("atlas_placement_duration_seconds", "s.labels['result'] AS series"),
		"Line",
		time_series(
			measure("Max s", "value", "max"),
			granularity="hour",
			number_format={"decimals": 3, "suffix": " s"},
		),
		"Host selection time. The target is under 100 ms.",
	)

	layout = Layout(workbook, "d-region", "Atlas - Region")
	layout.filter(
		"Period",
		"Date",
		workbook.filter_links([fleet, errors, actions, migrations, placement], "ts"),
		default_operator="within",
		default_value={"span": "last 1 day (include current)"},
	)
	layout.new_row()
	layout.heading("Region Health")
	layout.chart(health, 20, 20)
	layout.heading("SLA")
	layout.chart(SLA_CHART, 20, 10)
	layout.heading("Platform Services")
	layout.readings(SERVICE_STATUS_CHART, [name for name, _ in STATUS_READINGS])
	for heading, names in (
		("Fleet", ["Hosts Reporting", "Running VMs", "Pending Changes", "Failing VMs"]),
		("Atlas", ["Atlas Errors/hour", "Failed Jobs/hour", "Queued Jobs", "Metal Call Errors/hour"]),
		("Signals", ["Unhandled Errors/hour", "Host Errors/hour", "API 5xx/hour", "Worst Pool Used"]),
	):
		layout.heading(heading)
		for name in names:
			layout.reading(reading_charts[name], name)
	layout.heading("Capacity")
	for name in ("CPU Allocated", "Memory Allocated", "Storage Allocated"):
		layout.reading(fleet, name)
	layout.reading(hosts_with_issues, "Hosts With Issues")
	layout.heading("Public IPs")
	for name, width in (("IPv4 Total", 7), ("IPv4 In Use", 7), ("IPv4 Free", 6)):
		layout.reading(fleet, name, width)
	layout.heading("Hosts")
	layout.chart(hosts_table, 20, 12)
	layout.chart(hardware, 20, 12)
	layout.chart(stress, 20, 12)
	layout.heading("VM Actions")
	layout.readings(actions, [action.title() for action in ACTION_NAMES])
	layout.chart(migrations)
	layout.chart(placement)
	layout.heading("Migrations and Problems")
	layout.chart(active_migrations, 20, 12)
	layout.chart(problems, 20, 16)
