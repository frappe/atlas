from uptime_queries import (
	DAYS,
	MONTH_START,
	UPTIME_AXIS,
	VM_DOWNTIME_THIS_MONTH,
	VM_UPTIME_TARGET,
	daily_lines,
	vm_downtime_minutes_sql,
)
from workbook import TITLES, UPTIME_PERCENT, Layout, Workbook, dimension, measure, table

TITLE = "if(t.title = '', u.server, t.title)"


def host_minutes_sql(start: str) -> str:
	"""Report host-minutes after the first report, excluding fleet-wide telemetry gaps."""
	return f"""WITH
running AS (
  SELECT DISTINCT toStartOfMinute(ts) AS minute, labels['server'] AS server FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_metal_server_info' AND labels['status'] = 'Running' AND ts > {start}
),
reports AS (
  SELECT DISTINCT toStartOfMinute(ts) AS minute, resource_id FROM samples
  WHERE metric = 'metal_vm_pending' AND ts > {start}
),
first_reports AS (
  SELECT resource_id, min(minute) AS first_minute FROM reports GROUP BY resource_id
)
SELECT s.minute AS minute, s.server AS server, r.resource_id != '' AS reported
FROM running AS s
INNER JOIN first_reports AS f ON f.resource_id = s.server
LEFT JOIN reports AS r ON r.resource_id = s.server AND r.minute = s.minute
WHERE s.minute >= f.first_minute AND s.minute IN (SELECT minute FROM reports)"""


UPTIME_DAILY = f"""SELECT h.day AS day, h.host_uptime AS host_uptime, v.vm_uptime AS vm_uptime
FROM (
  SELECT toDate(minute) AS day, countIf(reported) / count() * 100 AS host_uptime FROM ({host_minutes_sql(DAYS)}) GROUP BY day
) AS h
LEFT JOIN (
  SELECT toDate(minute) AS day, (1 - sum(down) / nullIf(sum(wanted), 0)) * 100 AS vm_uptime
  FROM ({vm_downtime_minutes_sql(DAYS)}) GROUP BY day
) AS v ON v.day = h.day"""

HOST_UPTIME_DAILY = f"""WITH {TITLES}
SELECT toDate(u.minute) AS day, {TITLE} AS series, countIf(u.reported) / count() * 100 AS value
FROM ({host_minutes_sql(DAYS)}) AS u LEFT JOIN titles AS t ON t.server = u.server
GROUP BY day, series"""

VM_UPTIME_BY_HOST_DAILY = f"""WITH {TITLES}
SELECT toDate(u.minute) AS day, {TITLE} AS series, (1 - sum(u.down) / nullIf(sum(u.wanted), 0)) * 100 AS value
FROM ({vm_downtime_minutes_sql(DAYS)}) AS u LEFT JOIN titles AS t ON t.server = u.server
GROUP BY day, series"""

HOSTS_LEAST_UPTIME = f"""WITH {TITLES}
SELECT {TITLE} AS host, countIf(u.reported) / count() * 100 AS uptime, countIf(NOT u.reported) AS down_minutes
FROM ({host_minutes_sql(MONTH_START)}) AS u LEFT JOIN titles AS t ON t.server = u.server
GROUP BY host"""

HOSTS_LEAST_VM_UPTIME = f"""WITH {TITLES}
SELECT {TITLE} AS host, (1 - sum(u.down) / nullIf(sum(u.wanted), 0)) * 100 AS vm_uptime,
  sum(u.down) AS down_vm_minutes, max(u.wanted) AS wanted_vms
FROM ({vm_downtime_minutes_sql(MONTH_START)}) AS u LEFT JOIN titles AS t ON t.server = u.server
GROUP BY host"""

VMS_LEAST_UPTIME = f"""WITH {TITLES},
{VM_DOWNTIME_THIS_MONTH}
SELECT d.vm AS vm, if(t.title = '', d.resource_id, t.title) AS host, (1 - d.seconds / nullIf(m.seconds, 0)) * 100 AS uptime,
  d.seconds / 60 AS downtime_minutes, d.last_reason AS last_reason, d.is_down_now AS is_down_now
FROM downtime AS d
INNER JOIN measured AS m ON m.resource_id = d.resource_id
LEFT JOIN titles AS t ON t.server = d.resource_id
WHERE uptime < {VM_UPTIME_TARGET * 100:g}
ORDER BY uptime
LIMIT 100"""


def add_uptime_dashboard(workbook: Workbook) -> None:
	"""Uptime by day for hosts and VMs, then this month's hosts and VMs with the least uptime."""

	folder = workbook.folder("Uptime")
	overall = folder.add_chart(
		"uptime-overall",
		"Overall Uptime",
		UPTIME_DAILY,
		"Line",
		{
			"x_axis": {"dimension": dimension("day", "Date", granularity="day")},
			"y_axis": {
				"series": [
					{"measure": measure("VM Uptime", "vm_uptime", "min"), "color": ["#2563eb"]},
					{"measure": measure("Host Uptime", "host_uptime", "min"), "color": ["#16a34a"]},
				],
				**UPTIME_AXIS,
			},
			"number_formats": {"VM Uptime": UPTIME_PERCENT, "Host Uptime": UPTIME_PERCENT},
			"limit": 10000,
		},
		"VM uptime is the share of VM-minutes that VMs the user wants running were up. Host uptime is the share of minutes "
		"that Running hosts reported. Each point is one day.",
	)
	host_daily = folder.add_chart(
		"uptime-hosts",
		"Host Uptime by Host",
		HOST_UPTIME_DAILY,
		"Line",
		daily_lines("Host Uptime"),
		"The share of each day that a host reported to Datum.",
	)
	vm_daily = folder.add_chart(
		"uptime-vms-by-host",
		"VM Uptime by Host",
		VM_UPTIME_BY_HOST_DAILY,
		"Line",
		daily_lines("VM Uptime"),
		"The share of each day's VM-minutes that the VMs on a host were up.",
	)
	hosts_least = folder.add_chart(
		"uptime-hosts-least",
		"Hosts with the Least Uptime, this month",
		HOSTS_LEAST_UPTIME,
		"Table",
		table(
			[dimension("host")],
			[measure("Uptime %", "uptime", "min"), measure("Down Minutes", "down_minutes", "max", "Integer")],
			order="Uptime %",
			direction="asc",
		),
		"Minutes in which a Running host did not report, since its first report this month.",
	)
	hosts_least_vm = folder.add_chart(
		"uptime-hosts-least-vm",
		"Hosts with the Least VM Uptime, this month",
		HOSTS_LEAST_VM_UPTIME,
		"Table",
		table(
			[dimension("host")],
			[
				measure("VM Uptime %", "vm_uptime", "min"),
				measure("Down VM-Minutes", "down_vm_minutes", "max", "Integer"),
				measure("Wanted VMs", "wanted_vms", "max", "Integer"),
			],
			order="VM Uptime %",
			direction="asc",
		),
		"VM-minutes that the VMs on each host were down. A host that stopped reporting counts all its wanted VMs as down.",
	)
	vms_least = folder.add_chart(
		"uptime-vms-least",
		"VMs Below 99.9% Uptime, this month",
		VMS_LEAST_UPTIME,
		"Table",
		table(
			[dimension("vm"), dimension("host"), dimension("last_reason")],
			[
				measure("Uptime %", "uptime", "min"),
				measure("Downtime Minutes", "downtime_minutes", "max"),
				measure("Down Now", "is_down_now", "max", "Integer"),
			],
			order="Uptime %",
			direction="asc",
			limit=100,
		),
		"The 100 VMs with the least uptime this month, from metald's log. Downtime while the host itself was down shows in "
		"the host tables instead.",
	)

	layout = Layout(workbook, "d-uptime", "Atlas - Uptime")
	layout.heading("Overall")
	layout.chart(overall, 20, 16)
	layout.heading("By Host")
	layout.chart(host_daily)
	layout.chart(vm_daily)
	layout.heading("Least Uptime, this month")
	layout.chart(hosts_least)
	layout.chart(hosts_least_vm)
	layout.chart(vms_least, 20, 28)
