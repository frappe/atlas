from dataclasses import dataclass

from bmc_dashboard import BMC_HOSTS, SYSTEM
from host.summary import DISKS_NOW, POOLS_NOW
from region_tables import STRESS
from slo_dashboard import SLA_STATUS, SLO_STATUS
from workbook import ERRORS, LOOKBACK, TITLES, UNHEALTHY_DISKS, Workbook

# Alerts check only hosts that Atlas runs. A Pending, Installing, or Stopped host is expected to be quiet.
RUNNING_HOSTS = """running AS (
  SELECT labels['server'] AS server FROM samples
  WHERE metric = 'atlas_metal_server_info' AND ts > now() - INTERVAL 15 MINUTE
  GROUP BY server
  HAVING argMax(labels['status'], ts) = 'Running'
)"""

# Proxy, WireGuard gateway, and IPv6 router nodes that Atlas runs. Vector on each one pushes under the node name.
SERVICE_NODES = """service_nodes AS (
  SELECT labels['node'] AS node FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_service_node' AND labels['service'] != 'cargo'
    AND ts > now() - INTERVAL 15 MINUTE
  GROUP BY node
)"""

HOST_TITLE = "if(t.title = '', {column}, t.title)"
GROWTH = "greatest(max(value) - min(value), 0)"
# Keep 25% of each disk free. A ZFS mount shares its pool's space, which the pool checks cover.
DISK_FREE_WARNING = 25
DISK_FREE_CRITICAL = 10


@dataclass
class Check:
	"""One alert check. Its SQL returns a `host`, `value`, and `detail` row for each failure.

	The SQL can read `titles`, `running`, and `service_nodes`, which the alert query defines once to stay under the Insights SQL size limit."""

	title: str
	healthy: str
	sql: str


@dataclass
class Alert:
	"""One Insights Alert: a query of failing checks, sent to the webhook on a cron schedule."""

	key: str
	title: str
	icon: str
	cron: str
	checks: list[Check]


def latest_check(
	condition: str, limit: str, detail: str = "''", reading: str = "argMax(value, ts)", count: bool = False
) -> str:
	"""Running hosts whose newest reading of some series breaks limit, such as `l.reading > 90`."""
	value = "toFloat64(count())" if count else "max(l.reading)"
	return f"""SELECT {HOST_TITLE.format(column="l.resource_id")} AS host, {value} AS value,
  arrayStringConcat(arraySort(groupUniqArray(l.detail)), ', ') AS detail
FROM (
  SELECT resource_id, {detail} AS detail, {reading} AS reading FROM samples
  WHERE {condition} AND ts > now() - INTERVAL 5 MINUTE AND resource_id IN (SELECT server FROM running)
  GROUP BY resource_id, labels
) AS l LEFT JOIN titles AS t ON t.server = l.resource_id
WHERE {limit}
GROUP BY host"""


def growth_check(condition: str, window: str = "INTERVAL 1 HOUR", detail: str = "''") -> str:
	"""Running hosts whose counters grew in window."""
	return f"""SELECT {HOST_TITLE.format(column="g.resource_id")} AS host, sum(g.increase) AS value,
  arrayStringConcat(arraySort(groupUniqArray(g.detail)), ', ') AS detail
FROM (
  SELECT resource_id, {detail} AS detail, {GROWTH} AS increase FROM samples
  WHERE {condition} AND ts > now() - {window} AND resource_id IN (SELECT server FROM running)
  GROUP BY resource_id, labels
) AS g LEFT JOIN titles AS t ON t.server = g.resource_id
WHERE g.increase > 0
GROUP BY host"""


def ratio_check(numerator: str, denominator: str, percent: int) -> str:
	"""Running hosts where the newest numerator is over percent of the newest denominator."""
	return f"""SELECT {HOST_TITLE.format(column="r.resource_id")} AS host, r.share AS value, '' AS detail
FROM (
  SELECT resource_id, argMaxIf(value, ts, metric = '{numerator}') / nullIf(argMaxIf(value, ts, metric = '{denominator}'), 0) * 100 AS share
  FROM samples
  WHERE metric IN ('{numerator}', '{denominator}') AND ts > now() - INTERVAL 5 MINUTE AND resource_id IN (SELECT server FROM running)
  GROUP BY resource_id
) AS r LEFT JOIN titles AS t ON t.server = r.resource_id
WHERE r.share > {percent}"""


def atlas_check(
	metric: str, reading: str, limit: str, detail: str = "''", window: str = "INTERVAL 15 MINUTE"
) -> str:
	"""Atlas series that break limit. reading aggregates one series, such as its newest value or growth."""
	return f"""SELECT 'Atlas' AS host, reading AS value, detail FROM (
  SELECT {detail} AS detail, {reading} AS reading FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric = '{metric}' AND ts > now() - {window}
  GROUP BY labels
)
WHERE {limit}"""


# The resources, free-space metric, and size metric of Metal host and service node file systems.
HOST_FILESYSTEMS = (
	"SELECT server FROM running",
	"system_filesystem_available_bytes",
	"system_filesystem_size_bytes",
)
SERVICE_FILESYSTEMS = (
	"SELECT node FROM service_nodes",
	"service_filesystem_free_bytes",
	"service_filesystem_total_bytes",
)


def disk_check(percent: int, resources: str, free_metric: str, size_metric: str) -> str:
	"""Resources with a mounted file system that has less than percent free."""
	return f"""SELECT {HOST_TITLE.format(column="f.resource_id")} AS host, min(f.free) AS value,
  arrayStringConcat(arraySort(groupArray(f.mountpoint)), ', ') AS detail
FROM (
  SELECT resource_id, labels['mountpoint'] AS mountpoint,
    argMaxIf(value, ts, metric = '{free_metric}') / nullIf(argMaxIf(value, ts, metric = '{size_metric}'), 0) * 100 AS free
  FROM samples
  WHERE metric IN ('{free_metric}', '{size_metric}') AND labels['type'] != 'zfs'
    AND ts > now() - INTERVAL 5 MINUTE AND resource_id IN ({resources})
  GROUP BY resource_id, mountpoint
) AS f LEFT JOIN titles AS t ON t.server = f.resource_id
WHERE f.free < {percent}
GROUP BY host"""


def atlas_disk_check(percent: int) -> str:
	"""Atlas file systems with less than percent free."""
	return f"""SELECT 'Atlas' AS host, free AS value, path AS detail
FROM (
  SELECT labels['path'] AS path,
    argMaxIf(value, ts, labels['kind'] = 'available') / nullIf(argMaxIf(value, ts, labels['kind'] = 'size'), 0) * 100 AS free
  FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_filesystem_bytes' AND ts = (
    SELECT max(ts) FROM samples WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_filesystem_bytes' AND ts > now() - INTERVAL 15 MINUTE
  )
  GROUP BY path
)
WHERE free < {percent}"""


def problems(*names: tuple[str, str]) -> str:
	"""Join the names of the problems whose condition holds, such as `CPU pressure, RAM`."""
	items = ", ".join(f"if(ifNull({condition}, 0), '{name}', '')" for name, condition in names)
	return f"arrayStringConcat(arrayFilter(x -> x != '', [{items}]), ', ')"


HOST_NOT_REPORTING = f"""WITH pushes AS (
  SELECT resource_id, max(ts) AS last_ts FROM samples
  WHERE metric = 'metal_vm_pending' AND ts > now() - INTERVAL 1 DAY AND resource_id IN (SELECT server FROM running)
  GROUP BY resource_id
)
SELECT {HOST_TITLE.format(column="r.server")} AS host,
  if(p.resource_id = '', 86400, dateDiff('second', p.last_ts, now())) AS value, '' AS detail
FROM running AS r LEFT JOIN pushes AS p ON p.resource_id = r.server
LEFT JOIN titles AS t ON t.server = r.server
WHERE value >= 180"""

ATLAS_NOT_REPORTING = """SELECT 'Atlas' AS host, dateDiff('second', max(ts), now()) AS value, '' AS detail
FROM samples WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_metal_servers' AND ts > now() - INTERVAL 1 DAY
HAVING value >= 180"""

# Atlas syncs each Running host every 10 seconds, and reports the age of the last good sync from the last hour.
# A host without a reported age has not synced for an hour. When Atlas itself is quiet, Atlas not reporting alerts.
HOST_NOT_SYNCED = f"""WITH atlas_push AS (
  SELECT max(ts) AS last_ts FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_metal_servers' AND ts > now() - INTERVAL 3 MINUTE
)
SELECT {HOST_TITLE.format(column="r.server")} AS host, if(s.server = '', 3600, s.age) AS value,
  if(s.server = '', 'no sync in the last hour', '') AS detail
FROM running AS r
LEFT JOIN (
  SELECT labels['server'] AS server, value AS age FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_host_sync_age_seconds' AND ts = (SELECT last_ts FROM atlas_push)
) AS s ON s.server = r.server
LEFT JOIN titles AS t ON t.server = r.server
WHERE value > 120 AND (SELECT count() FROM atlas_push WHERE last_ts > now() - INTERVAL 3 MINUTE) > 0"""

# metald pushes every 30 seconds. A VM counts when every push of the last 2 minutes reports it unavailable.
VMS_UNAVAILABLE = f"""SELECT {HOST_TITLE.format(column="p.resource_id")} AS host, min(p.down) AS value,
  arrayStringConcat(arraySort(groupUniqArrayArray(p.reasons)), ', ') AS detail
FROM (
  SELECT resource_id, ts, sum(value) AS down, groupArrayIf(labels['reason'], value > 0) AS reasons FROM samples
  WHERE metric = 'metal_vm_unavailable' AND ts > now() - INTERVAL 2 MINUTE AND resource_id IN (SELECT server FROM running)
  GROUP BY resource_id, ts
) AS p LEFT JOIN titles AS t ON t.server = p.resource_id
GROUP BY host
HAVING value > 0"""

SERVICE_NOT_READY = f"""SELECT n.node AS host, toFloat64(n.probes) AS value, concat(n.service, ' on ', {HOST_TITLE.format(column="n.server")}) AS detail
FROM (
  SELECT labels['node'] AS node, any(labels['service']) AS service, any(labels['server']) AS server, count() AS probes
  FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_service_ready' AND ts > now() - INTERVAL 3 MINUTE
  GROUP BY node
  HAVING max(value) = 0 AND probes >= 2
) AS n LEFT JOIN titles AS t ON t.server = n.server"""

API_SERVER_ERRORS = """SELECT 'Atlas' AS host, server_errors / requests * 100 AS value,
  concat(toString(server_errors), ' of ', toString(requests), ' requests') AS detail
FROM (
  SELECT sumIf(increase, status LIKE '5%') AS server_errors, sum(increase) AS requests
  FROM (
    SELECT labels['status'] AS status, greatest(max(value) - min(value), 0) AS increase FROM samples
    WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_api_requests_total' AND ts > now() - INTERVAL 15 MINUTE
    GROUP BY resource_id, labels
  )
)
WHERE requests >= 50 AND server_errors / requests > 0.01"""

LINKS_DOWN = f"""SELECT {HOST_TITLE.format(column="n.resource_id")} AS host, toFloat64(count()) AS value,
  arrayStringConcat(arraySort(groupArray(n.interface)), ', ') AS detail
FROM (
  SELECT resource_id, labels['interface'] AS interface,
    argMaxIf(value, ts, metric = 'system_network_up') AS up, argMaxIf(value, ts, metric = 'system_network_carrier') AS carrier
  FROM samples
  WHERE metric IN ('system_network_up', 'system_network_carrier') AND ts > now() - INTERVAL 5 MINUTE
    AND resource_id IN (SELECT server FROM running)
  GROUP BY resource_id, interface
) AS n LEFT JOIN titles AS t ON t.server = n.resource_id
WHERE n.up = 1 AND n.carrier = 0
GROUP BY host"""

UNHEALTHY_DISK_HOSTS = f"""WITH {UNHEALTHY_DISKS}
SELECT {HOST_TITLE.format(column="d.resource_id")} AS host, toFloat64(d.unhealthy_disks) AS value, '' AS detail
FROM disks AS d LEFT JOIN titles AS t ON t.server = d.resource_id
WHERE d.unhealthy_disks > 0 AND d.resource_id IN (SELECT server FROM running)"""

HOST_STRESS = f"""SELECT host, toFloat64(stress_signals) AS value, {
	problems(
		("CPU pressure", "cpu_pressure > 10"),
		("memory pressure", "memory_pressure > 5"),
		("I/O pressure", "io_pressure > 10"),
		("RAM over 90%", "ram_used > 90"),
		("load per core over 1.5", "load_per_core > 1.5"),
		("CPU over-allocated", "cpu_allocated > 100"),
		("memory over-allocated", "memory_allocated > 100"),
	)
} AS detail
FROM ({STRESS})"""

BMC_PROBLEMS = f"""SELECT host, toFloat64(problems) AS value, {
	problems(
		("unreachable", "reachable = 0"),
		("not read for 15 min", "minutes_since_read >= 15"),
		("unhealthy component", "unhealthy_components > 0"),
	)
} AS detail
FROM ({BMC_HOSTS})
WHERE reachable = 0 OR minutes_since_read >= 15 OR unhealthy_components > 0"""

# Atlas reads each BMC every 5 minutes and keeps each event's own time, so an event lands up to about 6 minutes late.
# An IPMI record names its kind in Event_Type, or only in its message ID. A Redfish event has a readable message.
BMC_EVENT_WINDOWS = {
	"critical": "l.ts > now() - INTERVAL 15 MINUTE",
	# The window ends 10 minutes before the hour, so each hourly run reports each event once.
	"warning": "l.ts >= toStartOfHour(now()) - INTERVAL 70 MINUTE AND l.ts < toStartOfHour(now()) - INTERVAL 10 MINUTE",
}


def bmc_events(level: str) -> str:
	"""Running hosts with new BMC events of one level, with the kind of each event."""
	return f"""SELECT {HOST_TITLE.format(column="l.resource_id")} AS host, toFloat64(count()) AS value,
  arrayStringConcat(arraySort(groupUniqArray(concat(l.attributes['sensor_type'], ': ',
    multiIf(extract(l.message, 'Event_Type : ([^,]+)') != '', extract(l.message, 'Event_Type : ([^,]+)'),
      l.message LIKE '%Record_Type : %', concat(l.attributes['log'], ' ', l.attributes['message_id']), left(l.message, 80))))),
    '; ') AS detail
FROM logs AS l LEFT JOIN titles AS t ON t.server = l.resource_id
WHERE l.source = 'bmc' AND l.level = '{level}' AND {BMC_EVENT_WINDOWS[level]} AND l.resource_id IN (SELECT server FROM running)
GROUP BY host"""


# metald logs one line per failed attempt, so a burst of one message is a real problem, not a retry.
REPEATED_HOST_ERRORS = f"""SELECT {HOST_TITLE.format(column="l.resource_id")} AS host, toFloat64(count()) AS value,
  concat(l.service, ': ', left(l.message, 100)) AS detail
FROM logs AS l LEFT JOIN titles AS t ON t.server = l.resource_id
WHERE l.ts > now() - INTERVAL 1 HOUR AND l.level IN ('error', 'critical', 'alert', 'emergency')
  AND (l.resource_id IN (SELECT server FROM running) OR l.resource_id IN (SELECT node FROM service_nodes))
GROUP BY host, l.service, l.message
HAVING value >= 10"""

# A node that never pushed has no telemetry installed yet, so it does not alert.
SERVICE_NOT_REPORTING = f"""SELECT p.resource_id AS host, dateDiff('second', p.last_ts, now()) AS value, '' AS detail
FROM (
  SELECT resource_id, max(ts) AS last_ts FROM samples
  WHERE metric = 'service_load5' AND ts > now() - {LOOKBACK} AND resource_id IN (SELECT node FROM service_nodes)
  GROUP BY resource_id
) AS p
WHERE value >= 180"""

SERVICE_MEMORY = """SELECT resource_id AS host,
  (1 - argMaxIf(value, ts, metric = 'service_memory_available_bytes') / nullIf(argMaxIf(value, ts, metric = 'service_memory_total_bytes'), 0)) * 100 AS value,
  '' AS detail
FROM samples
WHERE metric IN ('service_memory_available_bytes', 'service_memory_total_bytes') AND ts > now() - INTERVAL 5 MINUTE
  AND resource_id IN (SELECT node FROM service_nodes)
GROUP BY resource_id
HAVING value > 90"""

SERVICE_OOM_KILLS = """SELECT resource_id AS host, greatest(max(value) - min(value), 0) AS value, '' AS detail
FROM samples
WHERE metric = 'service_memory_oom_kill_events_total' AND ts > now() - INTERVAL 1 HOUR
  AND resource_id IN (SELECT node FROM service_nodes)
GROUP BY resource_id
HAVING value > 0"""

# An unreachable site VM also gives 5xx, so a high share on one node is often one broken site.
PROXY_SERVER_ERRORS = """SELECT resource_id AS host, sumIf(increase, status_class = '5xx') / sum(increase) * 100 AS value,
  concat(toString(sumIf(increase, status_class = '5xx')), ' of ', toString(sum(increase)), ' requests') AS detail
FROM (
  SELECT resource_id, labels['status_class'] AS status_class, greatest(max(value) - min(value), 0) AS increase FROM samples
  WHERE metric = 'service_proxy_requests_total' AND ts > now() - INTERVAL 1 HOUR AND resource_id IN (SELECT node FROM service_nodes)
  GROUP BY resource_id, labels
)
GROUP BY resource_id
HAVING sum(increase) >= 100 AND value > 5"""

# A fingerprint first seen in the last hour is new. A known one alerts only when it bursts.
ATLAS_ERRORS = f"""SELECT 'Atlas' AS host, toFloat64(countIf(ts > now() - INTERVAL 1 HOUR)) AS value,
  concat(if(min(ts) > now() - INTERVAL 1 HOUR, 'new: ', ''), any(left(message, 100))) AS detail
FROM logs
WHERE {ERRORS} AND attributes['unhandled'] = '1' AND ts > now() - INTERVAL 7 DAY
GROUP BY if(attributes['fingerprint'] = '', message, attributes['fingerprint'])
HAVING value > 0 AND (min(ts) > now() - INTERVAL 1 HOUR OR value >= 20)"""

FAILED_MIGRATIONS = f"""SELECT {HOST_TITLE.format(column="l.resource_id")} AS host, toFloat64(count()) AS value,
  arrayStringConcat(groupUniqArray(concat(l.attributes['virtual_machine_id'], ' ', left(l.attributes['error'], 80))), '; ') AS detail
FROM logs AS l LEFT JOIN titles AS t ON t.server = l.resource_id
WHERE l.message = 'migration state changed' AND l.attributes['to'] = 'failed' AND l.ts > now() - INTERVAL 1 HOUR
GROUP BY host"""

ATLAS_HOST_RAM = f"""SELECT {HOST_TITLE.format(column="m.server")} AS host, m.allocated AS value, '' AS detail
FROM (
  SELECT labels['server'] AS server,
    (1 - argMaxIf(value, ts, labels['kind'] = 'available') / nullIf(argMaxIf(value, ts, labels['kind'] = 'total'), 0)) * 100 AS allocated
  FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_host_memory_bytes' AND ts > now() - INTERVAL 15 MINUTE
  GROUP BY server
) AS m LEFT JOIN titles AS t ON t.server = m.server
WHERE m.server IN (SELECT server FROM running) AND m.allocated > 90"""

# Regions without a public IPv4 pool report a capacity of 0 and do not alert.
FREE_IPV4 = """SELECT 'Atlas' AS host, capacity - used AS value, concat(toString(used), ' of ', toString(capacity), ' in use') AS detail
FROM (
  SELECT sumIf(value, metric = 'atlas_public_ip_capacity') AS capacity,
    sumIf(value, metric = 'atlas_public_ip_allocations' AND labels['status'] != 'Available') AS used
  FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric IN ('atlas_public_ip_capacity', 'atlas_public_ip_allocations') AND labels['version'] = '4'
    AND ts = (SELECT max(ts) FROM samples WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_public_ip_capacity' AND ts > now() - INTERVAL 15 MINUTE)
)
WHERE capacity > 0 AND capacity - used < 5"""

HEARTBEAT_CHECK = "Heartbeat, hosts reporting"

# Always one row, so the report arrives every 6 hours even when all is well. A missing report means alerts are broken.
HEARTBEAT = """SELECT 'Atlas' AS host, toFloat64(uniqExactIf(resource_id, ts > now() - INTERVAL 5 MINUTE)) AS value,
  concat('of ', toString((SELECT count() FROM running)), ' running hosts reported in the last 5 min') AS detail
FROM samples
WHERE metric = 'metal_vm_pending' AND ts > now() - INTERVAL 5 MINUTE AND resource_id IN (SELECT server FROM running)"""

FLEET_ALLOCATION = """SELECT 'Atlas' AS host, allocated AS value, kind AS detail
FROM (
  SELECT replaceRegexpOne(metric, '^atlas_host_(cpu|memory|storage)_.*', '\\\\1') AS kind,
    (1 - sumIf(value, labels['kind'] = 'available') / nullIf(sumIf(value, labels['kind'] = 'total'), 0)) * 100 AS allocated
  FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric IN ('atlas_host_cpu_millicores', 'atlas_host_memory_bytes', 'atlas_host_storage_bytes')
    AND ts = (SELECT max(ts) FROM samples WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_host_memory_bytes' AND ts > now() - INTERVAL 15 MINUTE)
  GROUP BY kind
)
WHERE allocated > 80"""

VERSION_DRIFT = f"""WITH versions AS (
  SELECT resource_id, argMax(labels['version'], ts) AS version FROM samples
  WHERE metric = 'metal_build_info' AND ts > now() - INTERVAL 15 MINUTE AND resource_id IN (SELECT server FROM running)
  GROUP BY resource_id
)
SELECT v.version AS host, toFloat64(count()) AS value,
  arrayStringConcat(arraySort(groupArray({HOST_TITLE.format(column="v.resource_id")})), ', ') AS detail
FROM versions AS v LEFT JOIN titles AS t ON t.server = v.resource_id
WHERE (SELECT uniqExact(version) FROM versions) > 1
GROUP BY v.version"""

P1 = Alert(
	"critical",
	"Atlas P1 Critical",
	"🔴",
	"*/5 * * * *",
	[
		Check("Host not reporting, seconds", "under 180", HOST_NOT_REPORTING),
		Check("Atlas not reporting, seconds", "under 180", ATLAS_NOT_REPORTING),
		Check("Atlas cannot sync host, seconds", "at most 120", HOST_NOT_SYNCED),
		Check("Unavailable VMs", "0", VMS_UNAVAILABLE),
		Check("Failing VMs", "0", latest_check("metric = 'metal_vm_failing'", "l.reading > 0")),
		Check(
			"ZFS pool unhealthy",
			"healthy",
			latest_check("metric = 'system_zfs_pool_healthy'", "l.reading = 0", "labels['pool']"),
		),
		Check(
			"ZFS data errors",
			"0",
			latest_check("metric = 'system_zfs_pool_data_errors'", "l.reading > 0", "labels['pool']"),
		),
		Check(
			"Degraded RAID arrays",
			"0",
			latest_check("metric = 'system_md_degraded'", "l.reading > 0", "labels['device']", count=True),
		),
		Check(
			"Read-only file systems",
			"0",
			latest_check(
				"metric = 'system_filesystem_read_only'", "l.reading > 0", "labels['mountpoint']", count=True
			),
		),
		Check(
			"Host disk free, %",
			f"at least {DISK_FREE_CRITICAL}",
			disk_check(DISK_FREE_CRITICAL, *HOST_FILESYSTEMS),
		),
		Check(
			"Service node disk free, %",
			f"at least {DISK_FREE_CRITICAL}",
			disk_check(DISK_FREE_CRITICAL, *SERVICE_FILESYSTEMS),
		),
		Check("Atlas disk free, %", f"at least {DISK_FREE_CRITICAL}", atlas_disk_check(DISK_FREE_CRITICAL)),
		Check(
			"Pool used, %",
			"at most 90",
			latest_check("metric = 'system_zfs_pool_capacity_percent'", "l.reading > 90", "labels['pool']"),
		),
		Check(
			"Reconciler stalled, seconds",
			"at most 300, image loop 7200",
			# The image loop runs every hour, and earlier when an image changes.
			latest_check(
				"metric = 'metal_reconciler_last_pass_timestamp_seconds'",
				"l.reading > if(l.detail = 'image', 7200, 300)",
				"labels['loop']",
				"toUnixTimestamp(now()) - argMax(value, ts)",
			),
		),
		Check(
			"Oldest pending VM change, seconds",
			"at most 600",
			latest_check("metric = 'metal_vm_oldest_pending_seconds'", "l.reading > 600"),
		),
		Check("Service node not ready, failed probes", "0", SERVICE_NOT_READY),
		Check("Service node not reporting, seconds", "under 180", SERVICE_NOT_REPORTING),
		Check(
			"Uncorrected ECC errors, last 15 min",
			"0",
			growth_check(
				"metric = 'system_edac_uncorrectable_errors_total' AND labels['csrow'] = ''",
				"INTERVAL 15 MINUTE",
			),
		),
		Check("Critical BMC events, last 15 min", "0", bmc_events("critical")),
		Check("API 5xx, % of requests in 15 min", "at most 1", API_SERVER_ERRORS),
	],
)

# P2 runs as two alerts, because one query for every hourly check is over the Insights SQL size limit.
P2_HOSTS = Alert(
	"warning",
	"Atlas P2 Hosts",
	"🟠",
	"0 * * * *",
	[
		Check(
			"Failed systemd units",
			"0",
			latest_check(
				"metric = 'system_systemd_unit_state' AND labels['state'] = 'failed'",
				"l.reading > 0",
				"labels['unit']",
				count=True,
			),
		),
		Check(
			"Unit restarts, last hour",
			"0",
			growth_check("metric = 'system_systemd_unit_restarts_total'", detail="labels['unit']"),
		),
		Check(
			"Host disk free, %",
			f"at least {DISK_FREE_WARNING}",
			disk_check(DISK_FREE_WARNING, *HOST_FILESYSTEMS),
		),
		Check("Unhealthy disks", "0", UNHEALTHY_DISK_HOSTS),
		Check(
			"Corrected ECC errors, last hour",
			"0",
			growth_check("metric = 'system_edac_correctable_errors_total' AND labels['csrow'] = ''"),
		),
		Check("CPU throttles, last hour", "0", growth_check("metric = 'system_cpu_core_throttles_total'")),
		Check(
			"CPU temperature, °C",
			"at most 85",
			latest_check(
				"metric = 'system_hwmon_temperature_celsius' AND labels['chip'] = 'coretemp' AND labels['attribute'] = 'input'",
				"l.reading > 85",
			),
		),
		Check("Links down", "0", LINKS_DOWN),
		Check(
			"NIC errors, last hour",
			"0",
			growth_check(
				"metric IN ('system_network_receive_errors_total', 'system_network_transmit_errors_total', 'system_network_rx_crc_errors_total')",
				detail="labels['interface']",
			),
		),
		Check(
			"ZFS slow I/O, last hour",
			"0",
			growth_check("metric = 'system_zfs_device_slow_ios'", detail="labels['device']"),
		),
		Check("Host stress signals", "0", HOST_STRESS),
		Check(
			"Conntrack table used, %",
			"at most 80",
			ratio_check("system_network_conntrack_entries", "system_network_conntrack_entries_limit", 80),
		),
		Check(
			"Open files used, %",
			"at most 80",
			ratio_check("system_open_files", "system_open_files_limit", 80),
		),
		Check(
			"Clock not synced", "synced", latest_check("metric = 'system_timex_sync_status'", "l.reading = 0")
		),
		Check("BMC problems", "0", BMC_PROBLEMS),
		Check("BMC warning events, last hour", "0", bmc_events("warning")),
		Check("Repeated host errors, last hour", "under 10", REPEATED_HOST_ERRORS),
		Check(
			"Dropped telemetry, last hour",
			"0",
			growth_check(
				"metric IN ('metal_telemetry_observations_dropped_total', 'metal_telemetry_collector_errors_total')",
				detail="if(labels['collector'] = '', 'dropped', labels['collector'])",
			),
		),
		Check("RAM allocated, %", "at most 90", ATLAS_HOST_RAM),
	],
)

P2_PLATFORM = Alert(
	"warning-platform",
	"Atlas P2 Platform",
	"🟠",
	"0 * * * *",
	[
		Check(
			"Service node disk free, %",
			f"at least {DISK_FREE_WARNING}",
			disk_check(DISK_FREE_WARNING, *SERVICE_FILESYSTEMS),
		),
		Check("Service node memory used, %", "at most 90", SERVICE_MEMORY),
		Check("Service node OOM kills, last hour", "0", SERVICE_OOM_KILLS),
		Check("Proxy 5xx, % of requests in the last hour", "at most 5", PROXY_SERVER_ERRORS),
		Check("Atlas disk free, %", f"at least {DISK_FREE_WARNING}", atlas_disk_check(DISK_FREE_WARNING)),
		Check("Unhandled Atlas errors, last hour", "no new error, under 20", ATLAS_ERRORS),
		Check("Failed migrations, last hour", "0", FAILED_MIGRATIONS),
		Check(
			"Oldest active migration, seconds",
			"at most 3600",
			atlas_check("atlas_migration_oldest_active_seconds", "argMax(value, ts)", "reading > 3600"),
		),
		Check(
			"Failed jobs, last hour",
			"0",
			atlas_check("atlas_job_failed", GROWTH, "reading > 0", "labels['queue']", "INTERVAL 1 HOUR"),
		),
		Check(
			"Queued jobs",
			"at most 100",
			atlas_check("atlas_job_queue_length", "argMax(value, ts)", "reading > 100", "labels['queue']"),
		),
		Check(
			"Failed Metal calls, last hour",
			"under 5",
			atlas_check(
				"atlas_metal_requests_total",
				GROWTH,
				"reading >= 5 AND detail NOT LIKE '2%'",
				"concat(labels['status'], ' ', labels['method'], ' ', labels['route'])",
				"INTERVAL 1 HOUR",
			),
		),
		Check("Free public IPv4 addresses", "at least 5", FREE_IPV4),
	],
)

P3 = Alert(
	"report",
	"Atlas P3 Report",
	"🔵",
	"0 */6 * * *",
	[
		Check(HEARTBEAT_CHECK, "every running host", HEARTBEAT),
		Check(
			"SLA error budget left, %",
			"at least 50",
			f"SELECT sla AS host, budget_left AS value, concat(toString(round(this_month, 2)), '% this month') AS detail FROM ({SLA_STATUS}) WHERE budget_left < 50",
		),
		Check(
			"SLO error budget left, %",
			"at least 50",
			f"SELECT slo AS host, budget_left AS value, concat(toString(round(within_target, 1)), '% within ', target) AS detail FROM ({SLO_STATUS}) WHERE budget_left < 50",
		),
		Check("Fleet allocated, %", "at most 80", FLEET_ALLOCATION),
		Check(
			"Pool used, %",
			"at most 75",
			latest_check("metric = 'system_zfs_pool_capacity_percent'", "l.reading > 75", "labels['pool']"),
		),
		Check("metald versions, hosts", "one version", VERSION_DRIFT),
		Check(
			"BMC temperature headroom, °C",
			"at least 10",
			f"SELECT host, temperature_headroom AS value, '' AS detail FROM ({BMC_HOSTS}) WHERE temperature_headroom < 10",
		),
		Check(
			"BMC clock offset, minutes",
			"under 5",
			f"SELECT host, clock_offset_minutes AS value, '' AS detail FROM ({SYSTEM}) WHERE clock_offset_minutes >= 5",
		),
		Check(
			"Days since last scrub",
			"at most 35",
			f"SELECT host, scrub_age_days AS value, name AS detail FROM ({POOLS_NOW}) WHERE scrub_age_days > 35",
		),
		Check(
			"Disk life used, %",
			"at most 80",
			f"SELECT host, life_used AS value, concat(device, ' ', model) AS detail FROM ({DISKS_NOW}) WHERE life_used > 80",
		),
		Check(
			"Oldest VM draft, hours",
			"at most 24",
			atlas_check(
				"atlas_virtual_machine_oldest_draft_seconds", "argMax(value, ts) / 3600", "reading > 24"
			),
		),
	],
)

ALERTS = [P1, P2_HOSTS, P2_PLATFORM, P3]

# The query is the Atlas instance that pushes to this Datum, so alerts from several regions stay apart.
ATLAS_INSTANCE = f"""SELECT argMax(resource_id, ts) AS atlas FROM samples
WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_metal_servers' AND ts > now() - {LOOKBACK}"""

# Jinja that Insights renders with the query rows. The rows arrive in check order, so a heading starts each check.
MESSAGE_BODY = f""" · `{{{{ rows[0].atlas }}}}`
{{%- set failing = rows | rejectattr('check', 'equalto', '{HEARTBEAT_CHECK}') | list %}}
{{%- for row in rows if row.check == '{HEARTBEAT_CHECK}' %}}

{{{{ row.value | int }}}} {{{{ row.detail }}}}
{{%- endfor %}}
{{%- if not failing %}}
✅ All checks are healthy.
{{%- endif %}}
{{%- for row in failing %}}
{{%- if loop.changed(row.check) %}}

**{{{{ row.check }}}}** (healthy: {{{{ row.healthy }}}})
{{%- endif %}}
- `{{{{ row.host }}}}`: **{{{{ '%g' | format(row.value) }}}}**{{% if row.detail %}} · {{{{ row.detail }}}}{{% endif %}}
{{%- endfor %}}"""


def alert_sql(alert: Alert) -> str:
	"""One row for each failing host of each check, in check order."""
	branches = "\n  UNION ALL\n  ".join(
		f"SELECT {position} AS position, '{check.title}' AS check, '{check.healthy}' AS healthy, toString(host) AS host, "
		f"round(toFloat64(ifNull(value, 0)), 1) AS value, toString(detail) AS detail FROM ({check.sql})"
		for position, check in enumerate(alert.checks)
	)
	return f"""WITH {TITLES}, {RUNNING_HOSTS}, {SERVICE_NODES}
SELECT c.check AS check, c.host AS host, c.value AS value, c.healthy AS healthy, c.detail AS detail, a.atlas AS atlas
FROM (
  {branches}
) AS c CROSS JOIN ({ATLAS_INSTANCE}) AS a
ORDER BY c.position, c.value DESC"""


def add_alerts(workbook: Workbook) -> None:
	"""Add the alert queries to the workbook, and the alerts that send them."""
	for alert in ALERTS:
		query = workbook.query(f"q-alert-{alert.key}", alert.title, alert_sql(alert), "Alerts")
		workbook.alerts.append(
			{
				"title": alert.title,
				"query": query,
				"cron_format": alert.cron,
				"condition": "q['check'] != ''",
				"message": f"{alert.icon} **{alert.title}**{MESSAGE_BODY}",
			}
		)
