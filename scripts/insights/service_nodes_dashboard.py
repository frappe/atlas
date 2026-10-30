from host.charts import BITS_PER_SECOND, PER_SECOND, Chart
from host.dashboard import add_section_chart
from platform_services_dashboard import SERVICE_TITLE
from workbook import (
	COUNT,
	LOOKBACK,
	PERCENT,
	SERVICE_NODE_IDS,
	Layout,
	Workbook,
	dimension,
	measure,
	rate_sql,
	table,
)

MILLISECONDS = {"decimals": 0, "suffix": " ms"}


def pivot_sql(metrics: tuple[str, str], value: str, series: str = "''") -> str:
	"""One row per node, push, and series, from two gauges of the same push, such as free and total bytes."""
	names = ", ".join(f"'{metric}'" for metric in metrics)
	return f"""SELECT ts, resource_id AS host, {series} AS series, {value} AS value
FROM samples
WHERE metric IN ({names}) AND ts > now() - {LOOKBACK} AND resource_id IN ({SERVICE_NODE_IDS})
GROUP BY ts, host, series"""


CPU_BUSY = f"""SELECT ts, resource_id AS host, 'Busy' AS series,
  (1 - sumIf(increase, mode = 'idle') / nullIf(sum(increase), 0)) * 100 AS value
FROM (
  SELECT ts, resource_id, labels['mode'] AS mode, greatest(value - lagInFrame(value, 1, value) OVER w, 0) AS increase
  FROM samples
  WHERE metric = 'service_cpu_seconds_total' AND ts > now() - {LOOKBACK} AND resource_id IN ({SERVICE_NODE_IDS})
  WINDOW w AS (PARTITION BY resource_id, labels ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
GROUP BY ts, host
HAVING sum(increase) > 0"""

# Vector stamps each request counter with its request time, so one push holds several times. Sum them per minute.
PROXY_MINUTES = f"""SELECT toStartOfMinute(ts) AS ts, resource_id AS host,
  sumIf(increase, metric = 'service_proxy_requests_total') AS requests,
  sumIf(increase, metric = 'service_proxy_requests_total' AND status_class = '5xx') AS server_errors,
  sumIf(increase, metric = 'service_proxy_request_seconds_total') AS request_seconds,
  sumIf(increase, metric = 'service_proxy_upstream_seconds_total') AS upstream_seconds,
  sumIf(increase, metric = 'service_proxy_slow_requests_total') AS slow_requests
FROM (
  SELECT ts, resource_id, metric, labels['status_class'] AS status_class,
    greatest(value - lagInFrame(value, 1, value) OVER w, 0) AS increase
  FROM samples
  WHERE metric LIKE 'service_proxy_%' AND ts > now() - {LOOKBACK} AND resource_id IN ({SERVICE_NODE_IDS})
  WINDOW w AS (PARTITION BY resource_id, metric, labels ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
GROUP BY ts, host"""

PROXY_LATENCY = f"""SELECT ts, host, pair.1 AS series, pair.2 AS value
FROM ({PROXY_MINUTES})
ARRAY JOIN [('Request', request_seconds / requests * 1000), ('Upstream', upstream_seconds / requests * 1000)] AS pair
WHERE requests > 0"""

PROXY_SERVER_ERRORS = f"""SELECT ts, host, '5xx' AS series, server_errors / requests * 100 AS value
FROM ({PROXY_MINUTES})
WHERE requests > 0"""

PROXY_SLOW = f"SELECT ts, host, 'Over 1 s' AS series, slow_requests AS value FROM ({PROXY_MINUTES})"

# One row per node that Atlas runs now: its newest push, use, and the last day's OOM kills and proxy requests.
NODES_NOW = f"""WITH nodes AS (
  SELECT labels['node'] AS node, argMax(labels['service'], ts) AS service FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_service_node' AND labels['service'] != 'cargo'
    AND ts > now() - INTERVAL 15 MINUTE
  GROUP BY node
),
latest AS (
  SELECT resource_id, max(ts) AS last_ts,
    (1 - argMaxIf(value, ts, metric = 'service_memory_available_bytes')
      / nullIf(argMaxIf(value, ts, metric = 'service_memory_total_bytes'), 0)) * 100 AS memory_used,
    argMaxIf(value, ts, metric = 'service_load5') / greatest(argMaxIf(value, ts, metric = 'service_logical_cpus'), 1) AS load_per_cpu
  FROM samples
  WHERE metric LIKE 'service_%' AND ts > now() - INTERVAL 1 DAY AND resource_id IN (SELECT node FROM nodes)
  GROUP BY resource_id
),
disks AS (
  SELECT resource_id, min(free) AS worst_disk_free FROM (
    SELECT resource_id, labels['mountpoint'] AS mountpoint,
      argMaxIf(value, ts, metric = 'service_filesystem_free_bytes')
        / nullIf(argMaxIf(value, ts, metric = 'service_filesystem_total_bytes'), 0) * 100 AS free
    FROM samples
    WHERE metric IN ('service_filesystem_free_bytes', 'service_filesystem_total_bytes') AND ts > now() - INTERVAL 15 MINUTE
      AND resource_id IN (SELECT node FROM nodes)
    GROUP BY resource_id, mountpoint
  )
  GROUP BY resource_id
),
growth AS (
  SELECT resource_id, sumIf(increase, metric = 'service_memory_oom_kill_events_total') AS oom_kills,
    sumIf(increase, metric = 'service_proxy_requests_total') AS requests,
    sumIf(increase, metric = 'service_proxy_requests_total' AND status_class = '5xx') AS server_errors
  FROM (
    SELECT resource_id, metric, labels['status_class'] AS status_class, greatest(max(value) - min(value), 0) AS increase
    FROM samples
    WHERE metric IN ('service_memory_oom_kill_events_total', 'service_proxy_requests_total') AND ts > now() - INTERVAL 1 DAY
      AND resource_id IN (SELECT node FROM nodes)
    GROUP BY resource_id, metric, labels
  )
  GROUP BY resource_id
)
SELECT {SERVICE_TITLE} AS service, n.node AS node,
  if(l.resource_id = '', NULL, dateDiff('second', l.last_ts, now())) AS last_push_seconds,
  l.memory_used AS memory_used, l.load_per_cpu AS load_per_cpu, d.worst_disk_free AS worst_disk_free,
  g.oom_kills AS oom_kills, g.requests AS requests, g.server_errors / nullIf(g.requests, 0) * 100 AS server_error_share
FROM nodes AS n
LEFT JOIN latest AS l ON l.resource_id = n.node
LEFT JOIN disks AS d ON d.resource_id = n.node
LEFT JOIN growth AS g ON g.resource_id = n.node"""

PROXY_CHARTS = [
	Chart(
		key="node-proxy-requests",
		title="Proxy Requests by Status",
		sql=rate_sql(
			["service_proxy_requests_total"], "d.labels['status_class']", resources=SERVICE_NODE_IDS
		),
		measure="Requests/s",
		aggregation="avg",
		number_format=PER_SECOND,
	),
	Chart(
		key="node-proxy-server-errors",
		title="Proxy 5xx Share",
		sql=PROXY_SERVER_ERRORS,
		measure="5xx %",
		aggregation="avg",
		number_format=PERCENT,
		info="5xx answers are often an unreachable site VM, not a proxy fault. A rise on every proxy node points at the proxy.",
	),
	Chart(
		key="node-proxy-latency",
		title="Proxy Average Time per Request",
		sql=PROXY_LATENCY,
		measure="Time",
		aggregation="avg",
		number_format=MILLISECONDS,
		info="Request is the whole request in the proxy. Upstream is the wait for the site VM that answered.",
	),
	Chart(
		key="node-proxy-slow",
		title="Proxy Requests over 1 s per Minute",
		sql=PROXY_SLOW,
		measure="Requests",
		aggregation="sum",
		number_format=COUNT,
	),
]

RESOURCE_CHARTS = [
	Chart(
		key="node-cpu",
		title="CPU Busy",
		sql=CPU_BUSY,
		measure="Busy",
		aggregation="avg",
		number_format=PERCENT,
	),
	Chart(
		key="node-memory",
		title="Memory Used",
		sql=pivot_sql(
			("service_memory_available_bytes", "service_memory_total_bytes"),
			"(1 - sumIf(value, metric = 'service_memory_available_bytes') / nullIf(sumIf(value, metric = 'service_memory_total_bytes'), 0)) * 100",
			"'Used'",
		),
		measure="Used",
		aggregation="avg",
		number_format=PERCENT,
	),
	Chart(
		key="node-disk-free",
		title="Disk Free by Mount",
		sql=pivot_sql(
			("service_filesystem_free_bytes", "service_filesystem_total_bytes"),
			"sumIf(value, metric = 'service_filesystem_free_bytes') / nullIf(sumIf(value, metric = 'service_filesystem_total_bytes'), 0) * 100",
			"labels['mountpoint']",
		),
		measure="Free",
		aggregation="min",
		number_format=PERCENT,
		info="Keep at least 25% of each disk free. The alerts warn under 25% and page under 10%.",
	),
	Chart(
		key="node-load",
		title="Load per CPU, 5 min",
		sql=pivot_sql(
			("service_load5", "service_logical_cpus"),
			"sumIf(value, metric = 'service_load5') / greatest(sumIf(value, metric = 'service_logical_cpus'), 1)",
			"'Load'",
		),
		measure="Load",
		aggregation="avg",
		number_format={"decimals": 2},
	),
	Chart(
		key="node-network",
		title="Network Traffic",
		sql=rate_sql(
			["service_network_receive_bytes_total", "service_network_transmit_bytes_total"],
			"concat(if(d.metric = 'service_network_receive_bytes_total', 'recv ', 'trans '), d.labels['device'])",
			scale="8",
			resources=SERVICE_NODE_IDS,
		),
		measure="Bits/s",
		aggregation="avg",
		number_format=BITS_PER_SECOND,
	),
	Chart(
		key="node-network-errors",
		title="Network Errors and Drops",
		sql=rate_sql(
			[
				"service_network_receive_errs_total",
				"service_network_transmit_errs_total",
				"service_network_transmit_packets_drop_total",
			],
			"concat(replaceRegexpOne(d.metric, '^service_network_(\\\\w+?)_(errs|packets_drop)_total$', '\\\\1 \\\\2 '), d.labels['device'])",
			per_second=False,
			resources=SERVICE_NODE_IDS,
		),
		measure="Packets",
		aggregation="sum",
		number_format=COUNT,
	),
	Chart(
		key="node-oom-kills",
		title="OOM Kills",
		sql=rate_sql(
			["service_memory_oom_kill_events_total"],
			"'OOM kills'",
			per_second=False,
			resources=SERVICE_NODE_IDS,
		),
		measure="Kills",
		aggregation="sum",
		number_format=COUNT,
	),
]


def add_service_nodes_dashboard(workbook: Workbook) -> None:
	"""System use of each proxy, WireGuard gateway, and IPv6 router node, and the proxy's request traffic."""
	folder = workbook.folder("Service Nodes")
	nodes = folder.add_chart(
		"service-nodes-now",
		"Service Nodes Now",
		NODES_NOW,
		"Table",
		table(
			[dimension("service"), dimension("node")],
			[
				measure("Last Push, s", "last_push_seconds", "max", "Integer"),
				measure("Memory Used %", "memory_used", "max"),
				measure("Load per CPU", "load_per_cpu", "max"),
				measure("Worst Disk Free %", "worst_disk_free", "min"),
				measure("OOM Kills, last day", "oom_kills", "max", "Integer"),
				measure("Proxy Requests, last day", "requests", "max", "Integer"),
				measure("Proxy 5xx %", "server_error_share", "max"),
			],
			order="Worst Disk Free %",
			direction="asc",
		),
		"Vector on each node pushes every 30 seconds. A node without a push has no telemetry installed, or cannot reach Datum.",
	)
	proxy = [add_section_chart(folder, chart) for chart in PROXY_CHARTS]
	resources = [add_section_chart(folder, chart) for chart in RESOURCE_CHARTS]

	layout = Layout(workbook, "Atlas - Service Nodes")
	layout.filter(
		"Period",
		"Date",
		workbook.filter_links([*proxy, *resources], "ts"),
		default_operator="within",
		default_value={"span": "last 1 day (include current)"},
	)
	layout.filter(
		"Node", "String", workbook.filter_links([*proxy, *resources], "host"), default_operator="in"
	)
	layout.heading("Nodes Now")
	layout.chart(nodes, 20, 10)
	layout.heading("Proxy Traffic")
	for chart in proxy:
		layout.chart(chart)
	layout.heading("System")
	for chart in resources:
		layout.chart(chart)
