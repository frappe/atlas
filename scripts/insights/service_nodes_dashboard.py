from host.charts import BITS_PER_SECOND, PER_SECOND, Chart
from host.dashboard import add_section_chart
from platform_services_dashboard import SERVICE_TITLE
from workbook import (
	COUNT,
	HOST,
	LOOKBACK,
	PERCENT,
	SERVICE_NODE_IDS,
	TITLES,
	Layout,
	Workbook,
	dimension,
	machine_usage_sql,
	measure,
	rate_sql,
	table,
)

MILLISECONDS = {"decimals": 0, "suffix": " ms"}


def pivot_sql(metrics: tuple[str, str], value: str, series: str = "''") -> str:
	"""One row per node, push, and series, from two gauges of the same push, such as available and size bytes."""
	names = ", ".join(f"'{metric}'" for metric in metrics)
	return f"""WITH {TITLES}
SELECT s.ts AS ts, {HOST.format(alias="s")} AS host, {series} AS series, {value} AS value
FROM samples AS s LEFT JOIN titles AS t ON t.server = s.resource_id
WHERE s.metric IN ({names}) AND s.ts > now() - {LOOKBACK} AND s.resource_id IN ({SERVICE_NODE_IDS})
GROUP BY ts, host, series"""


def usage_sql(column: str, series: str) -> str:
	"""One row per node and push: one column of the machine usage."""
	return f"""WITH {TITLES}
SELECT u.ts AS ts, {HOST.format(alias="u")} AS host, '{series}' AS series, u.{column} AS value
FROM ({machine_usage_sql(f"ts > now() - {LOOKBACK}", SERVICE_NODE_IDS)}) AS u
LEFT JOIN titles AS t ON t.server = u.resource_id
WHERE u.{column} IS NOT NULL"""


# Vector stamps each request counter with its request time, so one push holds several times. Sum them per minute.
PROXY_MINUTES = f"""WITH {TITLES}
SELECT toStartOfMinute(d.ts) AS ts, {HOST.format(alias="d")} AS host,
  sumIf(d.increase, d.metric = 'proxy_requests_total') AS requests,
  sumIf(d.increase, d.metric = 'proxy_requests_total' AND d.status_class = '5xx') AS server_errors,
  sumIf(d.increase, d.metric = 'proxy_request_seconds_total') AS request_seconds,
  sumIf(d.increase, d.metric = 'proxy_upstream_seconds_total') AS upstream_seconds,
  sumIf(d.increase, d.metric = 'proxy_slow_requests_total') AS slow_requests
FROM (
  SELECT ts, resource_id, metric, labels['status_class'] AS status_class,
    greatest(value - lagInFrame(value, 1, value) OVER w, 0) AS increase
  FROM samples
  WHERE metric IN ('proxy_requests_total', 'proxy_request_seconds_total', 'proxy_upstream_seconds_total', 'proxy_slow_requests_total')
    AND ts > now() - {LOOKBACK} AND resource_id IN ({SERVICE_NODE_IDS})
  WINDOW w AS (PARTITION BY resource_id, metric, labels ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
) AS d LEFT JOIN titles AS t ON t.server = d.resource_id
GROUP BY ts, host"""

PROXY_LATENCY = f"""SELECT ts, host, pair.1 AS series, pair.2 AS value
FROM ({PROXY_MINUTES})
ARRAY JOIN [('Request', request_seconds / requests * 1000), ('Upstream', upstream_seconds / requests * 1000)] AS pair
WHERE requests > 0"""

PROXY_SERVER_ERRORS = f"""SELECT ts, host, '5xx' AS series, server_errors / requests * 100 AS value
FROM ({PROXY_MINUTES})
WHERE requests > 0"""

PROXY_SLOW = f"SELECT ts, host, 'Over 1 s' AS series, slow_requests AS value FROM ({PROXY_MINUTES})"

# One row per node that Atlas runs now: its newest push, use, and the last day's proxy requests.
NODES_NOW = f"""WITH nodes AS (
  SELECT labels['node'] AS node, argMax(labels['service'], ts) AS service, argMax(labels['virtual_machine'], ts) AS vm
  FROM samples
  WHERE resource_id = 'atlas' AND metric = 'atlas_service_node' AND labels['service'] != 'cargo'
    AND ts > now() - INTERVAL 15 MINUTE
  GROUP BY node
),
latest AS (
  SELECT resource_id, max(ts) AS last_ts,
    (1 - argMaxIf(value, ts, metric = 'system_memory_available_bytes')
      / nullIf(argMaxIf(value, ts, metric = 'system_memory_total_bytes'), 0)) * 100 AS memory_used,
    argMaxIf(value, ts, metric = 'system_load_average' AND labels['window'] = '5m')
      / greatest(argMaxIf(value, ts, metric = 'system_cpu_count'), 1) AS load_per_cpu
  FROM samples
  WHERE metric IN ('system_boot_time_seconds', 'system_memory_available_bytes', 'system_memory_total_bytes',
      'system_load_average', 'system_cpu_count')
    AND ts > now() - INTERVAL 1 DAY AND resource_id IN (SELECT vm FROM nodes)
  GROUP BY resource_id
),
disks AS (
  SELECT resource_id, min(free) AS worst_disk_free FROM (
    SELECT resource_id, labels['mountpoint'] AS mountpoint,
      argMaxIf(value, ts, metric = 'system_filesystem_available_bytes')
        / nullIf(argMaxIf(value, ts, metric = 'system_filesystem_size_bytes'), 0) * 100 AS free
    FROM samples
    WHERE metric IN ('system_filesystem_available_bytes', 'system_filesystem_size_bytes') AND ts > now() - INTERVAL 15 MINUTE
      AND resource_id IN (SELECT vm FROM nodes)
    GROUP BY resource_id, mountpoint
  )
  GROUP BY resource_id
),
requests AS (
  SELECT resource_id, sum(increase) AS requests, sumIf(increase, status_class = '5xx') AS server_errors
  FROM (
    SELECT resource_id, labels['status_class'] AS status_class, greatest(max(value) - min(value), 0) AS increase
    FROM samples
    WHERE metric = 'proxy_requests_total' AND ts > now() - INTERVAL 1 DAY AND resource_id IN (SELECT vm FROM nodes)
    GROUP BY resource_id, labels
  )
  GROUP BY resource_id
)
SELECT {SERVICE_TITLE} AS service, n.node AS node,
  if(l.resource_id = '', NULL, dateDiff('second', l.last_ts, now())) AS last_push_seconds,
  l.memory_used AS memory_used, l.load_per_cpu AS load_per_cpu, d.worst_disk_free AS worst_disk_free,
  r.requests AS requests, r.server_errors / nullIf(r.requests, 0) * 100 AS server_error_share
FROM nodes AS n
LEFT JOIN latest AS l ON l.resource_id = n.vm
LEFT JOIN disks AS d ON d.resource_id = n.vm
LEFT JOIN requests AS r ON r.resource_id = n.vm"""

PROXY_CHARTS = [
	Chart(
		key="node-proxy-requests",
		title="Proxy Requests by Status",
		sql=rate_sql(["proxy_requests_total"], "d.labels['status_class']", resources=SERVICE_NODE_IDS),
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
		sql=usage_sql("cpu_busy", "Busy"),
		measure="Busy",
		aggregation="avg",
		number_format=PERCENT,
	),
	Chart(
		key="node-memory",
		title="Memory Used",
		sql=usage_sql("ram_used", "Used"),
		measure="Used",
		aggregation="avg",
		number_format=PERCENT,
	),
	Chart(
		key="node-disk-free",
		title="Disk Free by Mount",
		sql=pivot_sql(
			("system_filesystem_available_bytes", "system_filesystem_size_bytes"),
			"sumIf(s.value, s.metric = 'system_filesystem_available_bytes') / nullIf(sumIf(s.value, s.metric = 'system_filesystem_size_bytes'), 0) * 100",
			"s.labels['mountpoint']",
		),
		measure="Free",
		aggregation="min",
		number_format=PERCENT,
		info="Keep at least 25% of each disk free. Beacon alerts under 25% and pages under 10%.",
	),
	Chart(
		key="node-load",
		title="Load per CPU, 5 min",
		sql=pivot_sql(
			("system_load_average", "system_cpu_count"),
			"sumIf(s.value, s.metric = 'system_load_average' AND s.labels['window'] = '5m') / greatest(sumIf(s.value, s.metric = 'system_cpu_count'), 1)",
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
			["system_network_receive_bytes_total", "system_network_transmit_bytes_total"],
			"concat(if(d.metric = 'system_network_receive_bytes_total', 'recv ', 'trans '), d.labels['interface'])",
			scale="8",
			resources=SERVICE_NODE_IDS,
		),
		measure="Bits/s",
		aggregation="avg",
		number_format=BITS_PER_SECOND,
	),
	Chart(
		key="node-network-errors",
		title="Network Errors",
		sql=rate_sql(
			["system_network_receive_errors_total", "system_network_transmit_errors_total"],
			"concat(if(d.metric = 'system_network_receive_errors_total', 'recv ', 'trans '), d.labels['interface'])",
			per_second=False,
			resources=SERVICE_NODE_IDS,
		),
		measure="Packets",
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
				measure("Proxy Requests, last day", "requests", "max", "Integer"),
				measure("Proxy 5xx %", "server_error_share", "max"),
			],
			order="Worst Disk Free %",
			direction="asc",
		),
		"The Datum agent on each node pushes every 30 seconds. A node without a push has no agent installed, or cannot reach Datum.",
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
