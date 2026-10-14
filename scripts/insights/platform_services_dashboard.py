from uptime_queries import DAYS, MONTH_START, VM_DOWNTIME_THIS_MONTH, daily_lines
from workbook import TITLES, Layout, Workbook, dimension, measure, numbers, table, time_series

# Atlas - Region shows the readings of this chart too.
STATUS_CHART = "c-service-status"

# The active nodes as of Atlas's newest push, and each node's readiness probes this month.
# Atlas probes once a minute, so the share of ready probes is the share of ready minutes.
SERVICE_NODES = f"""service_nodes AS (
  SELECT labels['service'] AS service, labels['node'] AS node, labels['virtual_machine'] AS vm, labels['server'] AS server
  FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_service_node' AND ts = (
    SELECT max(ts) FROM samples WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_service_node' AND ts > now() - INTERVAL 1 HOUR
  )
),
readiness AS (
  SELECT labels['node'] AS node, argMax(value, ts) AS ready_now, avg(value) * 100 AS ready_uptime, countIf(value = 0) AS failed_checks
  FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_service_ready' AND ts > {MONTH_START}
  GROUP BY node
),
{VM_DOWNTIME_THIS_MONTH}"""

# A host that stopped pushing logs nothing for its VMs, so they count as down.
VM_UP_NOW = "if(dateDiff('second', m.last_push, now()) >= 120, 0, if(d.vm = '', 1, NOT d.is_down_now))"

SERVICE_TITLE = (
	"multiIf(n.service = 'proxy', 'Proxy', n.service = 'wireguard_gateway', 'WG Gateway', "
	"n.service = 'ipv6_router', 'IPv6 Router', 'Cargo')"
)

# A VM without an availability log line this month was up all month. The IPv6 router
# has no readiness URL, so its uptime is its VM uptime.
SERVICE_NODES_TABLE = f"""WITH {TITLES},
{SERVICE_NODES}
SELECT {SERVICE_TITLE} AS service, n.node AS node, n.vm AS vm, if(t.title = '', n.server, t.title) AS host,
  {VM_UP_NOW} AS vm_up_now,
  if(d.vm = '', 100, (1 - d.seconds / nullIf(m.seconds, 0)) * 100) AS vm_uptime,
  if(d.vm = '', 0, d.seconds / 60) AS vm_down_minutes,
  if(r.node = '', 'VM ping', 'readiness URL') AS measured_by,
  if(r.node = '', vm_up_now, r.ready_now) AS up_now,
  if(r.node = '', vm_uptime, r.ready_uptime) AS uptime,
  r.failed_checks AS failed_checks
FROM service_nodes AS n
LEFT JOIN readiness AS r ON r.node = n.node
LEFT JOIN downtime AS d ON d.vm = n.vm
LEFT JOIN measured AS m ON m.resource_id = n.server
LEFT JOIN titles AS t ON t.server = n.server"""

# The share of each service's nodes that are ready now. The IPv6 router has no
# readiness URL, so it reads the share of its VMs that are up.
SERVICE_STATUS = f"""WITH {SERVICE_NODES}
SELECT
  avgIf(r.ready_now, n.service = 'proxy') * 100 AS proxy_ready,
  avgIf(r.ready_now, n.service = 'wireguard_gateway') * 100 AS gateway_ready,
  avgIf({VM_UP_NOW}, n.service = 'ipv6_router') * 100 AS router_up,
  avgIf(r.ready_now, n.service = 'cargo') * 100 AS cargo_ready
FROM service_nodes AS n
LEFT JOIN readiness AS r ON r.node = n.node
LEFT JOIN downtime AS d ON d.vm = n.vm
LEFT JOIN measured AS m ON m.resource_id = n.server"""

STATUS_READINGS = [
	("Proxy Ready", "proxy_ready", "#2563eb"),
	("WG Gateway Ready", "gateway_ready", "#16a34a"),
	("IPv6 Router Up", "router_up", "#7c3aed"),
	("Cargo Ready", "cargo_ready", "#f59e0b"),
]

READINESS_DAILY = f"""SELECT toDate(ts) AS day, labels['node'] AS series, avg(value) * 100 AS value
FROM samples
WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_service_ready' AND ts > {DAYS}
GROUP BY day, series"""

FAILED_CHECKS = """SELECT toStartOfHour(ts) AS ts, labels['node'] AS series, countIf(value = 0) AS value
FROM samples
WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_service_ready' AND ts > now() - INTERVAL 7 DAY
GROUP BY ts, series"""


def add_platform_services_dashboard(workbook: Workbook) -> None:
	"""Service status now, each node's readiness and VM uptime this month, then readiness by day."""

	folder = workbook.folder("Platform Services")
	status = folder.add_chart(
		"service-status",
		"Platform Services",
		SERVICE_STATUS,
		"Number",
		{
			**numbers(
				[measure(name, column, "max") for name, column, _ in STATUS_READINGS],
				colors=[color for _, _, color in STATUS_READINGS],
			),
			"number_formats": {name: {"decimals": 0, "suffix": "%"} for name, _, _ in STATUS_READINGS},
		},
		"The share of each service's nodes that answer their readiness URL now. The IPv6 router has none, so it "
		"shows the share of its VMs that are up.",
	)
	nodes = folder.add_chart(
		"service-nodes",
		"Service Nodes, this month",
		SERVICE_NODES_TABLE,
		"Table",
		table(
			[
				dimension("service"),
				dimension("node"),
				dimension("vm"),
				dimension("host"),
				dimension("measured_by"),
			],
			[
				measure("Up Now", "up_now", "max", "Integer"),
				measure("Uptime %", "uptime", "min"),
				measure("VM Uptime %", "vm_uptime", "min"),
				measure("VM Down Minutes", "vm_down_minutes", "max"),
				measure("Failed Checks", "failed_checks", "max", "Integer"),
			],
			order="Uptime %",
			direction="asc",
		),
		"Atlas probes each node's readiness URL every minute. VM uptime comes from metald's availability checks. The IPv6 "
		"router has no readiness URL, so its uptime is its VM uptime.",
	)
	daily = folder.add_chart(
		"service-readiness-daily",
		"Readiness by Node, last 28 days",
		READINESS_DAILY,
		"Line",
		daily_lines("Ready %"),
		"The share of each day's readiness probes that a node answered.",
	)
	failed = folder.add_chart(
		"service-failed-checks",
		"Failed Readiness Checks per Hour, last 7 days",
		FAILED_CHECKS,
		"Line",
		time_series(measure("Failed Checks", "value", "sum", "Integer"), granularity="hour"),
		"Probes that timed out after 3 seconds or did not answer with a success status.",
	)

	layout = Layout(workbook, "d-platform-services", "Atlas - Platform Services")
	layout.heading("Now")
	layout.readings(status, [name for name, *_ in STATUS_READINGS])
	layout.heading("This Month")
	layout.chart(nodes, 20, 12)
	layout.heading("Trend")
	layout.chart(daily)
	layout.chart(failed)
