from workbook import HOST, TITLES, UNHEALTHY_DISKS

# One row per host: its newest push, Atlas's view of its capacity, and a count of
# the problem kinds it has, so the worst host sorts first. metald omits a VM state
# with no VMs, so VM counts read the newest push and an absent state reads 0.
FLEET_HOSTS = f"""WITH {TITLES},
latest AS (
  SELECT resource_id, max(ts) AS last_ts,
    argMaxIf(value, ts, metric = 'system_cpu_usage_percent') AS cpu,
    argMaxIf(value, ts, metric = 'system_memory_usage_percent') AS memory,
    argMaxIf(value, ts, metric = 'system_zfs_pool_capacity_percent') AS pool,
    argMaxIf(value, ts, metric = 'system_zfs_pool_healthy') AS pool_healthy,
    argMaxIf(value, ts, metric = 'system_systemd_failed_units') AS failed_units,
    sumIf(value, metric = 'metal_vm_count' AND labels['state'] = 'running' AND ts = last_push_ts) AS running,
    sumIf(value, metric = 'metal_vm_count' AND labels['state'] != 'destroyed' AND ts = last_push_ts) AS total_vms,
    argMaxIf(value, ts, metric = 'metal_vm_pending') AS pending,
    argMaxIf(value, ts, metric = 'metal_vm_failing') AS failing,
    argMaxIf(labels['version'], ts, metric = 'metal_build_info') AS version
  FROM (
    SELECT *, max(if(metric = 'metal_vm_pending', ts, NULL)) OVER (PARTITION BY resource_id) AS last_push_ts
    FROM samples
    WHERE ts > now() - INTERVAL 15 MINUTE AND resource_id NOT LIKE 'atlas-%'
  )
  GROUP BY resource_id
  HAVING countIf(metric = 'metal_vm_pending') > 0
),
arrays AS (
  SELECT resource_id, sum(degraded) AS degraded_arrays
  FROM (
    SELECT resource_id, labels['device'] AS device, argMax(value, ts) > 0 AS degraded
    FROM samples
    WHERE metric = 'system_md_degraded' AND ts > now() - INTERVAL 15 MINUTE
    GROUP BY resource_id, device
  )
  GROUP BY resource_id
),
-- What Atlas has reserved for VMs on each host, from the newest host sync.
capacity AS (
  SELECT labels['server'] AS server,
    (1 - argMaxIf(value, ts, metric = 'atlas_host_cpu_millicores' AND labels['kind'] = 'available')
      / nullIf(argMaxIf(value, ts, metric = 'atlas_host_cpu_millicores' AND labels['kind'] = 'total'), 0)) * 100 AS cpu_allocated,
    (1 - argMaxIf(value, ts, metric = 'atlas_host_memory_bytes' AND labels['kind'] = 'available')
      / nullIf(argMaxIf(value, ts, metric = 'atlas_host_memory_bytes' AND labels['kind'] = 'total'), 0)) * 100 AS memory_allocated,
    (1 - argMaxIf(value, ts, metric = 'atlas_host_storage_bytes' AND labels['kind'] = 'available')
      / nullIf(argMaxIf(value, ts, metric = 'atlas_host_storage_bytes' AND labels['kind'] = 'total'), 0)) * 100 AS storage_allocated
  FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric LIKE 'atlas_host_%' AND ts > now() - INTERVAL 15 MINUTE
  GROUP BY server
),
{UNHEALTHY_DISKS}
SELECT {HOST.format(alias="l")} AS host, l.version AS version, dateDiff('second', l.last_ts, now()) AS last_push_seconds,
  l.cpu AS cpu, l.memory AS memory, l.pool AS pool, c.cpu_allocated AS cpu_allocated, c.memory_allocated AS memory_allocated,
  c.storage_allocated AS storage_allocated, l.total_vms AS total_vms, l.running AS running,
  l.pending AS pending, l.failing AS failing, l.failed_units AS failed_units, d.unhealthy_disks AS unhealthy_disks,
  toUInt8(dateDiff('second', l.last_ts, now()) >= 120) + toUInt8(l.failed_units > 0) + toUInt8(d.unhealthy_disks > 0)
    + toUInt8(l.pool_healthy = 0) + toUInt8(a.degraded_arrays > 0) AS issues,
  toUInt8(issues > 0) AS has_issues
FROM latest AS l LEFT JOIN titles AS t ON t.server = l.resource_id
LEFT JOIN arrays AS a ON a.resource_id = l.resource_id
LEFT JOIN capacity AS c ON c.server = l.resource_id
LEFT JOIN disks AS d ON d.resource_id = l.resource_id"""


# Problems of the last day, one row per kind of problem and place, newest first.
RECENT_PROBLEMS = f"""WITH {TITLES}
SELECT if(l.resource_id LIKE 'atlas-%', 'Atlas', if(t.title = '', l.resource_id, t.title)) AS place,
  multiIf(l.service = 'atlas', 'Unhandled error', l.message = 'migration state changed', 'Migration failed', 'Host error') AS kind,
  if(l.service = 'atlas', l.message, concat(l.service, ': ', l.message)) AS problem,
  any(left(coalesce(nullIf(l.attributes['error'], ''), l.attributes['exception'], ''), 160)) AS detail,
  count() AS occurrences, min(l.ts) AS first_seen, max(l.ts) AS last_seen
FROM logs AS l LEFT JOIN titles AS t ON t.server = l.resource_id
WHERE l.ts > now() - INTERVAL 1 DAY AND (
  (l.service = 'atlas' AND l.source = 'error_log' AND l.attributes['unhandled'] = '1')
  OR (l.resource_id NOT LIKE 'atlas-%' AND l.level IN ('error', 'critical', 'alert', 'emergency'))
  OR (l.message = 'migration state changed' AND l.attributes['to'] IN ('failed', 'aborted'))
)
GROUP BY place, kind, problem"""


# Counters that grew in the last day.
HARDWARE_COUNTERS = {
	"ecc_corrected": "metric = 'system_edac_correctable_errors_total' AND labels['csrow'] = ''",
	"ecc_uncorrected": "metric = 'system_edac_uncorrectable_errors_total' AND labels['csrow'] = ''",
	"thermal_throttles": "metric = 'system_cpu_core_throttles_total'",
	"nic_errors": "metric IN ('system_network_receive_errors_total', 'system_network_transmit_errors_total')",
}

HARDWARE = f"""WITH {TITLES},
latest AS (
  SELECT resource_id,
    argMaxIf(value, ts, metric = 'system_systemd_failed_units') AS failed_units,
    maxIf(value, metric = 'system_hwmon_temperature_celsius' AND labels['chip'] = 'coretemp' AND labels['attribute'] = 'input'
      AND ts > now() - INTERVAL 5 MINUTE) AS cpu_temperature,
    maxIf(value, metric = 'system_hwmon_temperature_celsius' AND labels['chip'] = 'jc42' AND labels['attribute'] = 'input'
      AND ts > now() - INTERVAL 5 MINUTE) AS memory_temperature,
    argMaxIf(value, ts, metric = 'system_zfs_pool_data_errors') AS zfs_data_errors,
    maxIf(value, metric = 'system_zfs_device_errors_maximum' AND ts > now() - INTERVAL 5 MINUTE) AS zfs_device_errors
  FROM samples
  WHERE ts > now() - INTERVAL 15 MINUTE AND resource_id NOT LIKE 'atlas-%'
  GROUP BY resource_id
  HAVING countIf(metric = 'system_systemd_failed_units') > 0
),
growth AS (
  SELECT resource_id, {", ".join(f"sumIf(increase, kind = '{name}') AS {name}" for name in HARDWARE_COUNTERS)}
  FROM (
    SELECT resource_id, multiIf({", ".join(f"{condition}, '{name}'" for name, condition in HARDWARE_COUNTERS.items())}, '') AS kind,
      greatest(max(value) - min(value), 0) AS increase
    FROM samples
    WHERE ts > now() - INTERVAL 1 DAY AND ({" OR ".join(f"({condition})" for condition in HARDWARE_COUNTERS.values())})
    GROUP BY resource_id, metric, labels
  )
  GROUP BY resource_id
),
-- An interface that is up but has no carrier lost its link.
links AS (
  SELECT resource_id, countIf(up = 1 AND carrier = 0) AS links_down
  FROM (
    SELECT resource_id, labels['interface'] AS interface,
      argMaxIf(value, ts, metric = 'system_network_up') AS up, argMaxIf(value, ts, metric = 'system_network_carrier') AS carrier
    FROM samples
    WHERE metric IN ('system_network_up', 'system_network_carrier') AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY resource_id, interface
  )
  GROUP BY resource_id
),
arrays AS (
  SELECT resource_id, countIf(degraded > 0) AS degraded_arrays
  FROM (
    SELECT resource_id, labels['device'] AS device, argMax(value, ts) AS degraded
    FROM samples WHERE metric = 'system_md_degraded' AND ts > now() - INTERVAL 15 MINUTE
    GROUP BY resource_id, device
  )
  GROUP BY resource_id
),
{UNHEALTHY_DISKS}
SELECT {HOST.format(alias="l")} AS host, coalesce(d.unhealthy_disks, 0) AS unhealthy_disks, coalesce(a.degraded_arrays, 0) AS degraded_arrays,
  l.zfs_data_errors AS zfs_data_errors, l.zfs_device_errors AS zfs_device_errors, g.ecc_corrected AS ecc_corrected,
  g.ecc_uncorrected AS ecc_uncorrected, l.cpu_temperature AS cpu_temperature, l.memory_temperature AS memory_temperature,
  g.thermal_throttles AS thermal_throttles, coalesce(n.links_down, 0) AS links_down, g.nic_errors AS nic_errors,
  l.failed_units AS failed_units,
  ifNull(unhealthy_disks > 0, 0) + ifNull(degraded_arrays > 0, 0) + ifNull(zfs_data_errors > 0, 0) + ifNull(zfs_device_errors > 0, 0)
    + ifNull(ecc_uncorrected > 0, 0) + ifNull(ecc_corrected > 0, 0) + ifNull(cpu_temperature > 85, 0) + ifNull(thermal_throttles > 0, 0)
    + ifNull(links_down > 0, 0) + ifNull(nic_errors > 0, 0) + ifNull(failed_units > 0, 0) AS problems
FROM latest AS l LEFT JOIN titles AS t ON t.server = l.resource_id
LEFT JOIN growth AS g ON g.resource_id = l.resource_id
LEFT JOIN links AS n ON n.resource_id = l.resource_id
LEFT JOIN arrays AS a ON a.resource_id = l.resource_id
LEFT JOIN disks AS d ON d.resource_id = l.resource_id
WHERE problems > 0"""

# Pressure is the share of the last 15 minutes that some tasks waited for a resource.
STRESS = f"""WITH {TITLES},
pressure AS (
  SELECT resource_id,
    maxIf(share, resource = 'cpu') AS cpu_pressure, maxIf(share, resource = 'memory') AS memory_pressure,
    maxIf(share, resource = 'io') AS io_pressure
  FROM (
    SELECT resource_id, labels['resource'] AS resource,
      (max(value) - min(value)) / nullIf(dateDiff('second', min(ts), max(ts)), 0) * 100 AS share
    FROM samples
    WHERE metric = 'system_pressure_stall_seconds_total' AND labels['kind'] = 'some' AND ts > now() - INTERVAL 15 MINUTE
    GROUP BY resource_id, labels
  )
  GROUP BY resource_id
),
usage AS (
  SELECT resource_id,
    avgIf(value, metric = 'system_cpu_usage_percent') AS cpu_busy,
    avgIf(value, metric = 'system_memory_usage_percent') AS ram_used,
    avgIf(value, metric = 'system_load_average' AND labels['window'] = '5m') / greatest(maxIf(value, metric = 'system_cpu_count'), 1) AS load_per_core
  FROM samples
  WHERE ts > now() - INTERVAL 15 MINUTE AND resource_id NOT LIKE 'atlas-%'
    AND metric IN ('system_cpu_usage_percent', 'system_memory_usage_percent', 'system_load_average', 'system_cpu_count')
  GROUP BY resource_id
),
allocation AS (
  SELECT labels['server'] AS server,
    (1 - argMaxIf(value, ts, metric = 'atlas_host_cpu_millicores' AND labels['kind'] = 'available')
      / nullIf(argMaxIf(value, ts, metric = 'atlas_host_cpu_millicores' AND labels['kind'] = 'total'), 0)) * 100 AS cpu_allocated,
    (1 - argMaxIf(value, ts, metric = 'atlas_host_memory_bytes' AND labels['kind'] = 'available')
      / nullIf(argMaxIf(value, ts, metric = 'atlas_host_memory_bytes' AND labels['kind'] = 'total'), 0)) * 100 AS memory_allocated
  FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric IN ('atlas_host_cpu_millicores', 'atlas_host_memory_bytes') AND ts > now() - INTERVAL 15 MINUTE
  GROUP BY server
)
SELECT {HOST.format(alias="u")} AS host, u.cpu_busy AS cpu_busy, u.load_per_core AS load_per_core, p.cpu_pressure AS cpu_pressure,
  p.memory_pressure AS memory_pressure, p.io_pressure AS io_pressure, u.ram_used AS ram_used,
  a.cpu_allocated AS cpu_allocated, a.memory_allocated AS memory_allocated,
  ifNull(cpu_pressure > 10, 0) + ifNull(memory_pressure > 5, 0) + ifNull(io_pressure > 10, 0) + ifNull(ram_used > 90, 0)
    + ifNull(load_per_core > 1.5, 0) + ifNull(cpu_allocated > 100, 0) + ifNull(memory_allocated > 100, 0) AS stress_signals
FROM usage AS u LEFT JOIN titles AS t ON t.server = u.resource_id
LEFT JOIN pressure AS p ON p.resource_id = u.resource_id
LEFT JOIN allocation AS a ON a.server = u.resource_id
WHERE stress_signals > 0"""

# Migrations whose newest step in the last day is not completed or aborted.
ACTIVE_MIGRATIONS = f"""WITH {TITLES}
SELECT m.migration_id AS migration, m.vm AS vm, if(t.title = '', m.resource_id, t.title) AS destination, m.step AS step,
  m.started AS started, dateDiff('second', m.started, now()) / 60 AS age_minutes, m.last_error AS last_error
FROM (
  SELECT attributes['migration_id'] AS migration_id, any(attributes['virtual_machine_id']) AS vm, argMax(resource_id, ts) AS resource_id,
    argMax(attributes['to'], ts) AS step, min(ts) AS started, argMax(attributes['error'], ts) AS last_error
  FROM logs
  WHERE message = 'migration state changed' AND ts > now() - INTERVAL 1 DAY
  GROUP BY migration_id
) AS m LEFT JOIN titles AS t ON t.server = m.resource_id
WHERE m.step NOT IN ('completed', 'aborted')"""


# One row per region check. Each check reads 0, or stays inside its limit, when the
# region is healthy.
REGION_HEALTH = f"""WITH
atlas_last AS (
  SELECT max(ts) AS last_ts FROM samples WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_metal_servers' AND ts > now() - INTERVAL 1 HOUR
),
atlas AS (
  SELECT metric, labels, value FROM samples
  WHERE resource_id LIKE 'atlas-%' AND ts = (SELECT last_ts FROM atlas_last)
),
host_last AS (
  SELECT resource_id, max(ts) AS last_ts,
    argMaxIf(value, ts, metric = 'metal_vm_failing') AS failing,
    argMaxIf(value, ts, metric = 'metal_vm_oldest_pending_seconds') AS oldest_pending
  FROM samples
  WHERE resource_id NOT LIKE 'atlas-%' AND ts > now() - INTERVAL 1 HOUR AND metric IN ('metal_vm_failing', 'metal_vm_oldest_pending_seconds')
  GROUP BY resource_id
),
api AS (
  SELECT sumIf(increase, status LIKE '5%') AS server_errors, sum(increase) AS requests
  FROM (
    SELECT labels['status'] AS status, greatest(max(value) - min(value), 0) AS increase
    FROM samples
    WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_api_requests_total' AND ts > now() - INTERVAL 1 HOUR
    GROUP BY labels
  )
),
checks AS (
  SELECT 'Hosts not reporting for 2 minutes' AS check, toFloat64(count()) AS value, 'at most' AS rule, 0 AS limit
  FROM (SELECT arrayJoin(groupArrayIf(labels['server'], metric = 'atlas_metal_server_info' AND labels['status'] = 'Running')) AS server FROM atlas) AS r
  LEFT JOIN host_last AS h ON h.resource_id = r.server
  WHERE h.last_ts < now() - INTERVAL 2 MINUTE OR h.resource_id = ''
  UNION ALL
  SELECT 'Hosts Atlas has not synced for 5 minutes', toFloat64(countIf(metric = 'atlas_host_sync_age_seconds' AND value > 300)), 'at most', 0 FROM atlas
  UNION ALL
  SELECT 'Hosts with issues', toFloat64(sum(has_issues)), 'at most', 0 FROM ({FLEET_HOSTS})
  UNION ALL
  SELECT 'Hosts with hardware problems', toFloat64(countIf(problems > 0)), 'at most', 0 FROM ({HARDWARE})
  UNION ALL
  SELECT 'Failing VMs', sum(failing), 'at most', 0 FROM host_last WHERE last_ts > now() - INTERVAL 5 MINUTE
  UNION ALL
  SELECT 'Service nodes not ready', toFloat64(countIf(metric = 'atlas_service_ready' AND value = 0)), 'at most', 0 FROM atlas
  UNION ALL
  SELECT 'Oldest pending VM change, seconds', max(oldest_pending), 'under', 300 FROM host_last WHERE last_ts > now() - INTERVAL 5 MINUTE
  UNION ALL
  SELECT 'Unhandled Atlas errors, last hour', toFloat64(count()), 'at most', 0 FROM logs
  WHERE service = 'atlas' AND source = 'error_log' AND attributes['unhandled'] = '1' AND ts > now() - INTERVAL 1 HOUR
  UNION ALL
  SELECT 'Failed migrations, last day', toFloat64(count()), 'at most', 0 FROM samples
  WHERE metric = 'atlas_migration_duration_seconds' AND labels['result'] = 'failed' AND ts > now() - INTERVAL 1 DAY
  UNION ALL
  SELECT 'API 5xx per 1,000 requests, last hour', coalesce(server_errors / nullIf(requests, 0) * 1000, 0), 'under', 1 FROM api
  UNION ALL
  SELECT 'Hosts with RAM over 90% allocated', toFloat64(count()), 'at most', 0 FROM (
    SELECT labels['server'] AS server, sumIf(value, labels['kind'] = 'available') / nullIf(sumIf(value, labels['kind'] = 'total'), 0) AS free
    FROM atlas WHERE metric = 'atlas_host_memory_bytes' GROUP BY server
  ) WHERE free < 0.1
  UNION ALL
  SELECT 'Free public IPv4 addresses',
    sumIf(value, metric = 'atlas_public_ip_capacity' AND labels['version'] = '4')
      - sumIf(value, metric = 'atlas_public_ip_allocations' AND labels['status'] != 'Available' AND labels['version'] = '4'),
    'over', 5 FROM atlas
)
SELECT if(multiIf(rule = 'under', value < limit, rule = 'over', value > limit, value <= limit), 'OK', 'ALERT') AS status,
  check, round(value, 1) AS value, if(rule = 'at most', toString(limit), concat(rule, ' ', toString(limit))) AS healthy
FROM checks"""
