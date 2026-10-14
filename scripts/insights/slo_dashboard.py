from uptime_queries import MONTH_START, VM_DOWNTIME_THIS_MONTH, VM_UPTIME_TARGET, vm_downtime_minutes_sql
from workbook import PERCENT, TITLES, Layout, Workbook, dimension, measure, table

WINDOW = "INTERVAL 28 DAY"
# Events are read this far back, so the oldest point of the trend has a full window.
HISTORY = "INTERVAL 56 DAY"
OBJECTIVE = 0.99


def observations_sql(metric: str, limit_seconds: float, where: str = "", good: str = "") -> str:
	"""Return observations with their latency and result checks."""
	return f"""SELECT ts, toUInt8(value <= {limit_seconds}{f" AND {good}" if good else ""}) AS good
FROM samples WHERE metric = '{metric}' AND ts > now() - {HISTORY} {where}"""


def counted_failures_sql(metric: str, where: str = "") -> str:
	"""One bad event for each increase of a failure counter."""
	return f"""SELECT ts, toUInt8(0) AS good
FROM (
  SELECT ts, greatest(value - lagInFrame(value, 1, value) OVER w, 0) AS increase
  FROM samples WHERE metric = '{metric}' AND ts > now() - {HISTORY} {where}
  WINDOW w AS (PARTITION BY resource_id, labels ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
ARRAY JOIN range(toUInt64(increase)) AS failure"""


def action(name: str) -> str:
	return f"AND labels['action'] = '{name}'"


# Name, target, and the events of each SLO. Each target holds for 99% of events.
SLOS = [
	("Create", "60 s", observations_sql("metal_vm_action_duration_seconds", 60, action("create"))),
	("Start", "30 s", observations_sql("metal_vm_action_duration_seconds", 30, action("start"))),
	("Stop", "15 s", observations_sql("metal_vm_action_duration_seconds", 15, action("stop"))),
	("Resize", "60 s", observations_sql("metal_vm_action_duration_seconds", 60, action("resize"))),
	("Terminate", "30 s", observations_sql("metal_vm_action_duration_seconds", 30, action("terminate"))),
	(
		"Snapshot",
		"15 min",
		observations_sql("atlas_image_create_duration_seconds", 900, good="labels['result'] = 'available'"),
	),
	(
		"Cold boot to ping",
		"60 s",
		observations_sql(
			"metal_vm_boot_seconds", 60, "AND labels['kind'] = 'cold' AND labels['stage'] = 'reachable'"
		)
		+ " UNION ALL "
		+ counted_failures_sql("metal_vm_boot_unreachable_total", "AND labels['kind'] = 'cold'"),
	),
	(
		"Warm boot to ping",
		"20 s",
		observations_sql(
			"metal_vm_boot_seconds", 20, "AND labels['kind'] = 'warm' AND labels['stage'] = 'reachable'"
		)
		+ " UNION ALL "
		+ counted_failures_sql("metal_vm_boot_unreachable_total", "AND labels['kind'] = 'warm'"),
	),
	# A migration the operator aborted is not a failure of the service.
	(
		"Migration",
		"10 min",
		observations_sql(
			"atlas_migration_duration_seconds",
			600,
			"AND labels['result'] != 'aborted'",
			"labels['result'] = 'completed'",
		),
	),
	("Migration downtime", "60 s", observations_sql("metal_migration_downtime_seconds", 60)),
]

SLO_EVENTS = " UNION ALL ".join(
	f"SELECT '{name}' AS slo, ts, good FROM ({events})" for name, _, events in SLOS
)

# Error budget: the share of events allowed to miss the target, 1% here. A budget
# below 0 means the SLO is breached.
SLO_STATUS = f"""WITH events AS (
  SELECT slo, count() AS events, sum(good) / count() AS within
  FROM ({SLO_EVENTS})
  WHERE ts > now() - {WINDOW}
  GROUP BY slo
)
SELECT d.slo AS slo, d.target AS target, '{OBJECTIVE * 100:g}%' AS objective, coalesce(e.events, 0) AS events,
  if(e.events > 0, e.within * 100, NULL) AS within_target,
  if(e.events > 0, greatest((1 - (1 - e.within) / {1 - OBJECTIVE:g}) * 100, -100), NULL) AS budget_left,
  multiIf(coalesce(e.events, 0) = 0, 'NO DATA', e.within >= {OBJECTIVE}, 'OK', 'BREACH') AS status
FROM values('slo String, target String', {", ".join(f"('{name}', '{target}')" for name, target, _ in SLOS)}) AS d
LEFT JOIN events AS e ON e.slo = d.slo"""

# For each of the last 28 days, the share within target over the 28 days up to it.
# Every SLO has a point for every day, empty when it had no events, so the chart
# lists the same SLOs as the status table.
SLO_DAILY = f"""WITH daily AS (
  SELECT slo, toDate(ts) AS day, count() AS events, sum(good) AS good
  FROM ({SLO_EVENTS})
  GROUP BY slo, day
),
rolling AS (
  SELECT d.day AS day, e.slo AS slo, sum(e.good) / sum(e.events) * 100 AS value
  FROM (SELECT today() - number AS day FROM numbers(28)) AS d
  CROSS JOIN daily AS e
  WHERE e.day <= d.day AND e.day > d.day - 28
  GROUP BY day, slo
)
SELECT d.day AS day, s.slo AS series, if(r.slo = '', NULL, r.value) AS value
FROM (SELECT today() - number AS day FROM numbers(28)) AS d
CROSS JOIN values('slo String', {", ".join(f"'{name}'" for name, _, _ in SLOS)}) AS s
LEFT JOIN rolling AS r ON r.day = d.day AND r.slo = s.slo"""


SLO_COLORS = [
	"#2563eb",
	"#16a34a",
	"#dc2626",
	"#f59e0b",
	"#7c3aed",
	"#0d9488",
	"#db2777",
	"#ea580c",
	"#64748b",
	"#0891b2",
]


def slo_column(name: str) -> str:
	return name.lower().replace(" ", "_")


SLO_DAILY_WIDE = f"""SELECT day, {", ".join(f"avgIf(value, series = '{name}') AS {slo_column(name)}" for name, _, _ in SLOS)}
FROM ({SLO_DAILY})
GROUP BY day"""


# Atlas - Region shows this chart too.
SLA_CHART = "c-sla-status"

SLA_STATUS = f"""WITH vm AS (
  SELECT sum(wanted) AS wanted_minutes, sum(down) AS down_minutes FROM ({vm_downtime_minutes_sql(MONTH_START)})
),
api AS (
  SELECT sumIf(increase, status LIKE '5%') AS server_errors, sum(increase) AS requests
  FROM (
    SELECT labels['status'] AS status, greatest(max(value) - min(value), 0) AS increase
    FROM samples
    WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_api_requests_total' AND ts > {MONTH_START}
    GROUP BY labels
  )
),
atlas_minutes AS (
  -- Minutes are counted from the first push of the month, when measuring began.
  SELECT uniqExact(toStartOfMinute(ts)) AS reported, dateDiff('minute', min(ts), now()) AS elapsed
  FROM samples WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_metal_servers' AND ts > {MONTH_START}
)
SELECT 'VM uptime' AS sla, '{VM_UPTIME_TARGET * 100:g}%' AS target,
  (1 - down_minutes / nullIf(wanted_minutes, 0)) * 100 AS this_month,
  greatest((1 - down_minutes / nullIf(wanted_minutes * {1 - VM_UPTIME_TARGET:g}, 0)) * 100, -100) AS budget_left,
  down_minutes AS downtime_vm_minutes
FROM vm
UNION ALL
SELECT 'Atlas API', '{VM_UPTIME_TARGET * 100:g}%',
  (1 - coalesce(server_errors / nullIf(requests, 0), 0)) * least(reported / nullIf(elapsed, 0), 1) * 100,
  greatest((1 - (1 - (1 - coalesce(server_errors / nullIf(requests, 0), 0)) * least(reported / nullIf(elapsed, 0), 1)) / {1 - VM_UPTIME_TARGET:g}) * 100, -100),
  NULL
FROM api CROSS JOIN atlas_minutes"""

# Per VM: the minutes it was unavailable this month, from metald's transition log.
# A host that stopped reporting logs nothing, so its VMs show in the host column instead.
VM_DOWNTIME = f"""WITH {TITLES},
{VM_DOWNTIME_THIS_MONTH}
SELECT d.vm AS vm, if(t.title = '', d.resource_id, t.title) AS host, d.periods AS periods, d.last_reason AS last_reason,
  d.seconds / 60 AS downtime_minutes, d.is_down_now AS is_down_now
FROM downtime AS d LEFT JOIN titles AS t ON t.server = d.resource_id"""


def add_slo_dashboard(workbook: Workbook) -> None:
	"""The SLA this month, the SLOs over 28 days, and the daily share of events within target."""
	folder = workbook.folder("SLA and SLOs")

	sla_table = folder.add_chart(
		"sla-status",
		"SLA, this month",
		SLA_STATUS,
		"Table",
		table(
			[dimension("sla"), dimension("target")],
			[
				measure("This Month %", "this_month", "max"),
				measure("Error Budget Left %", "budget_left", "max"),
				measure("Downtime VM-Minutes", "downtime_vm_minutes", "max"),
			],
			limit=10,
			scale="This Month %",
		),
		"VM uptime counts the minutes each VM the user wants running was down: stopped by a fault, stuck in a change, "
		"silent to ping for 3 minutes, or on a host that stopped reporting. API availability is the share of tenant API "
		"requests without a 5xx, times the share of minutes Atlas was up.",
	)
	downtime_table = folder.add_chart(
		"vm-downtime",
		"VM Downtime, this month",
		VM_DOWNTIME,
		"Table",
		table(
			[dimension("vm"), dimension("host"), dimension("last_reason")],
			[
				measure("Downtime Minutes", "downtime_minutes", "max"),
				measure("Periods", "periods", "sum", "Integer"),
				measure("Down Now", "is_down_now", "max", "Integer"),
			],
			order="Downtime Minutes",
			limit=100,
		),
		"VMs that were unavailable this month, longest first. A host outage shows on the SLA row, not here, because the host logs nothing.",
	)
	status_table = folder.add_chart(
		"slo-status",
		"SLOs, last 28 days",
		SLO_STATUS,
		"Table",
		table(
			[dimension("status"), dimension("slo"), dimension("target"), dimension("objective")],
			[
				measure("Events", "events", "sum", "Integer"),
				measure("Within Target %", "within_target", "max"),
				measure("Error Budget Left %", "budget_left", "max"),
			],
			limit=50,
			status_colors={"BREACH": "red", "OK": "green", "NO DATA": "amber"},
		),
		"Each SLO holds when 99% of its events are within target. Error Budget Left reaches -100 when the budget is spent twice. "
		"Actions run from the Metal API write to the applied state. Boots run until the guest answers ping.",
	)
	daily_chart = folder.add_chart(
		"slo-daily",
		"Within Target, Rolling 28 Days",
		SLO_DAILY_WIDE,
		"Line",
		{
			"x_axis": {"dimension": dimension("day", "Date", granularity="day")},
			"y_axis": {
				"series": [
					{"measure": measure(name, slo_column(name), "avg"), "color": [color]}
					for (name, _, _), color in zip(SLOS, SLO_COLORS, strict=True)
				],
				"min": 0,
				"max": 100,
				"smooth": True,
				"show_data_points": True,
			},
			"number_formats": {name: PERCENT for name, _, _ in SLOS},
			"limit": 10000,
		},
		"For each day, the share of each SLO's events within target over the 28 days up to that day.",
	)

	layout = Layout(workbook, "d-sla-slos", "Atlas - SLA and SLOs")
	layout.heading("Status")
	layout.chart(status_table, 20, 20)
	layout.heading("Trend")
	layout.chart(daily_chart, 20, 18)
	layout.heading("SLA")
	layout.chart(sla_table, 20, 10)
	layout.chart(downtime_table, 20, 16)
