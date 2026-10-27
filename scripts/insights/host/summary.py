from workbook import HOST, LOOKBACK, METAL_HOST_IDS, TITLES, UNHEALTHY_DISKS

HOST_QUICK = f"""WITH {TITLES},
latest AS (
  SELECT resource_id,
    argMaxIf(value, ts, metric = 'system_cpu_usage_percent') AS cpu_busy,
    argMaxIf(value, ts, metric = 'system_load_average' AND labels['window'] = '5m') AS load5,
    argMaxIf(value, ts, metric = 'system_load_average' AND labels['window'] = '15m') AS load15,
    argMaxIf(value, ts, metric = 'system_cpu_count') AS cores,
    argMaxIf(value, ts, metric = 'system_memory_usage_percent') AS ram_used,
    argMaxIf(value, ts, metric = 'system_memory_info' AND labels['field'] = 'MemTotal') AS ram_total,
    argMaxIf(value, ts, metric = 'system_memory_info' AND labels['field'] = 'SwapTotal') AS swap_total,
    argMaxIf(value, ts, metric = 'system_memory_info' AND labels['field'] = 'SwapFree') AS swap_free,
    argMaxIf(value, ts, metric = 'system_disk_usage_percent') AS root_used,
    argMaxIf(value, ts, metric = 'system_disk_total_bytes') AS root_total,
    argMaxIf(value, ts, metric = 'system_time_seconds') - argMaxIf(value, ts, metric = 'system_boot_time_seconds') AS uptime,
    maxIf(value, metric = 'system_hwmon_temperature_celsius' AND labels['chip'] = 'coretemp' AND labels['attribute'] = 'input'
      AND ts > now() - INTERVAL 2 MINUTE) AS cpu_temperature,
    maxIf(value, metric = 'system_hwmon_temperature_celsius' AND labels['chip'] = 'jc42' AND labels['attribute'] = 'input'
      AND ts > now() - INTERVAL 2 MINUTE) AS memory_temperature,
    argMaxIf(value, ts, metric = 'system_zfs_pool_size_bytes') AS pool_size,
    argMaxIf(value, ts, metric = 'system_systemd_failed_units') AS failed_units
  FROM samples
  WHERE ts > now() - INTERVAL 15 MINUTE AND resource_id IN ({METAL_HOST_IDS})
  GROUP BY resource_id
  HAVING countIf(metric = 'system_cpu_count') > 0
),
{UNHEALTHY_DISKS}
SELECT {HOST.format(alias="l")} AS host, l.cpu_busy AS cpu_busy,
  l.load5 / greatest(l.cores, 1) * 100 AS load5_percent, l.load15 / greatest(l.cores, 1) * 100 AS load15_percent,
  l.ram_used AS ram_used, if(l.swap_total > 0, (l.swap_total - l.swap_free) / l.swap_total * 100, 0) AS swap_used,
  l.root_used AS root_used, l.cores AS cores, l.ram_total / 1073741824 AS ram_total, l.swap_total / 1073741824 AS swap_total,
  l.root_total / 1073741824 AS root_total, l.uptime / 86400 AS uptime_days,
  l.cpu_temperature AS cpu_temperature, l.memory_temperature AS memory_temperature,
  l.pool_size / 1073741824 AS pool_size, l.failed_units AS failed_units, d.unhealthy_disks AS unhealthy_disks
FROM latest AS l LEFT JOIN titles AS t ON t.server = l.resource_id
LEFT JOIN disks AS d ON d.resource_id = l.resource_id"""

# Newest disk health per host and disk. smartctl runs every 10 minutes; NVMe
# disks are read directly. Life Used is the NVMe percentage used, or 100 minus
# the normalized SATA wear attribute.
DISKS_NOW = f"""WITH {TITLES},
latest AS (
  SELECT resource_id, labels['device'] AS device,
    argMaxIf(labels['model'], ts, metric IN ('system_smart_info', 'system_nvme_info')) AS model,
    if(countIf(metric = 'system_nvme_smart') > 0,
      toFloat64(argMaxIf(value, ts, metric = 'system_nvme_smart' AND labels['field'] = 'critical_warning') = 0),
      argMaxIfOrNull(value, ts, metric = 'system_smart_healthy')) AS healthy,
    argMaxIfOrNull(value, ts, metric = 'system_smart_exit_status') AS exit_status,
    argMaxIfOrNull(value, ts, metric = 'system_smart_temperature_celsius'
      OR (metric = 'system_nvme_smart' AND labels['field'] = 'temperature_celsius')) AS temperature,
    argMaxIfOrNull(value, ts, metric = 'system_smart_power_on_seconds'
      OR (metric = 'system_nvme_smart' AND labels['field'] = 'power_on_seconds')) / 86400 AS power_on_days,
    if(countIf(metric = 'system_nvme_smart') > 0,
      maxIfOrNull(value, metric = 'system_nvme_smart' AND labels['field'] = 'percentage_used'),
      100 - minIfOrNull(value, metric = 'system_smart_attribute' AND labels['id'] IN ('173', '177', '231', '233') AND labels['field'] = 'value')) AS life_used,
    maxIfOrNull(value, metric = 'system_smart_attribute' AND labels['id'] = '5' AND labels['field'] = 'raw') AS reallocated,
    maxIfOrNull(value, metric = 'system_smart_attribute' AND labels['id'] = '197' AND labels['field'] = 'raw') AS pending,
    maxIfOrNull(value, (metric = 'system_smart_attribute' AND labels['id'] IN ('187', '198') AND labels['field'] = 'raw')
      OR (metric = 'system_nvme_smart' AND labels['field'] = 'media_errors')) AS uncorrectable,
    argMaxIfOrNull(value, ts, metric = 'system_disk_scsi_timeouts_total') AS io_timeouts
  FROM samples
  WHERE ts > now() - INTERVAL 1 HOUR AND resource_id IN ({METAL_HOST_IDS}) AND labels['device'] != ''
    AND (metric LIKE 'system_smart_%' OR metric LIKE 'system_nvme_%' OR metric LIKE 'system_disk_scsi_%')
  GROUP BY resource_id, device
)
SELECT {HOST.format(alias="l")} AS host, l.device AS device, l.model AS model, l.healthy AS healthy, l.exit_status AS exit_status,
  l.temperature AS temperature, l.power_on_days AS power_on_days, l.life_used AS life_used, l.reallocated AS reallocated,
  l.pending AS pending, l.uncorrectable AS uncorrectable, l.io_timeouts AS io_timeouts
FROM latest AS l LEFT JOIN titles AS t ON t.server = l.resource_id"""

# Newest ZFS pool and software RAID health per host.
POOLS_NOW = f"""WITH {TITLES},
pools AS (
  SELECT resource_id, concat('zfs ', labels['pool']) AS name,
    argMaxIf(labels['state'], ts, metric = 'system_zfs_pool_state') AS state,
    argMaxIf(value, ts, metric = 'system_zfs_pool_data_errors') AS data_errors,
    maxIf(value, metric = 'system_zfs_device_errors_maximum' AND ts > now() - INTERVAL 2 MINUTE) AS device_errors,
    argMaxIf(concat(labels['function'], ' ', labels['state']), ts, metric = 'system_zfs_scan_state') AS scan,
    (toUnixTimestamp(now()) - argMaxIf(value, ts, metric = 'system_zfs_scan_end_timestamp_seconds' AND labels['function'] = 'scrub')) / 86400 AS scrub_age_days,
    argMaxIf(value, ts, metric = 'system_zfs_scan_errors' AND labels['function'] = 'scrub') AS scrub_errors,
    argMaxIf(value, ts, metric = 'system_zfs_scan_repaired_bytes' AND labels['function'] = 'scrub') / 1048576 AS repaired_mib
  FROM samples
  WHERE ts > now() - INTERVAL 15 MINUTE AND resource_id IN ({METAL_HOST_IDS}) AND metric LIKE 'system_zfs_%' AND labels['pool'] != ''
  GROUP BY resource_id, name
),
arrays AS (
  SELECT resource_id, concat('md ', labels['device']) AS name,
    argMaxIf(labels['state'], ts, metric = 'system_md_array_state') AS state,
    argMaxIf(value, ts, metric = 'system_md_mismatch_cnt') AS data_errors,
    maxIf(value, metric = 'system_md_member_corrected_errors' AND ts > now() - INTERVAL 2 MINUTE) AS device_errors,
    concat(argMaxIf(labels['action'], ts, metric = 'system_md_sync_action'), if(argMaxIf(value, ts, metric = 'system_md_degraded') > 0, ' degraded', '')) AS scan,
    CAST(NULL, 'Nullable(Float64)') AS scrub_age_days, CAST(NULL, 'Nullable(Float64)') AS scrub_errors, CAST(NULL, 'Nullable(Float64)') AS repaired_mib
  FROM samples
  WHERE ts > now() - INTERVAL 15 MINUTE AND resource_id IN ({METAL_HOST_IDS}) AND metric LIKE 'system_md_%' AND labels['device'] != ''
  GROUP BY resource_id, name
)
SELECT {HOST.format(alias="p")} AS host, p.name AS name, p.state AS state, p.data_errors AS data_errors, p.device_errors AS device_errors,
  p.scan AS scan, p.scrub_age_days AS scrub_age_days, p.scrub_errors AS scrub_errors, p.repaired_mib AS repaired_mib
FROM (SELECT * FROM pools UNION ALL SELECT * FROM arrays) AS p LEFT JOIN titles AS t ON t.server = p.resource_id"""

# One row per push: the readings the top cards plot as sparklines.
HOST_TREND = f"""WITH {TITLES},
pushes AS (
  SELECT ts, resource_id,
    anyIf(value, metric = 'system_cpu_usage_percent') AS cpu_busy,
    anyIf(value, metric = 'system_memory_usage_percent') AS ram_used,
    anyIf(value, metric = 'system_zfs_pool_capacity_percent') AS pool_used,
    anyIf(value, metric = 'system_disk_usage_percent') AS root_used,
    anyIf(value, metric = 'system_load_average' AND labels['window'] = '5m')
      / greatest(anyIf(value, metric = 'system_cpu_count'), 1) * 100 AS load5_percent,
    anyIf(value, metric = 'system_load_average' AND labels['window'] = '15m')
      / greatest(anyIf(value, metric = 'system_cpu_count'), 1) * 100 AS load15_percent,
    anyIf(value, metric = 'metal_vm_count' AND labels['state'] = 'running') AS running_vms
  FROM samples
  WHERE metric IN ('system_cpu_usage_percent', 'system_memory_usage_percent', 'system_zfs_pool_capacity_percent',
      'system_disk_usage_percent', 'system_load_average', 'system_cpu_count', 'metal_vm_count')
    AND ts > now() - {LOOKBACK} AND resource_id IN ({METAL_HOST_IDS})
  GROUP BY ts, resource_id
  HAVING countIf(metric = 'system_cpu_usage_percent') > 0
),
-- The busiest disk at each push: its share of the interval spent on I/O.
disks AS (
  SELECT ts, resource_id, max(busy) AS disk_busy
  FROM (
    SELECT ts, resource_id,
      greatest(value - lagInFrame(value, 1, value) OVER w, 0)
        / nullIf(dateDiff('millisecond', lagInFrame(ts, 1, ts) OVER w, ts) / 1000, 0) * 100 AS busy
    FROM samples
    WHERE metric = 'system_disk_io_time_seconds_total' AND ts > now() - {LOOKBACK} AND resource_id IN ({METAL_HOST_IDS})
    WINDOW w AS (PARTITION BY resource_id, labels ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
  )
  GROUP BY ts, resource_id
)
SELECT p.ts AS ts, {HOST.format(alias="p")} AS host, p.cpu_busy AS cpu_busy, p.ram_used AS ram_used,
  p.pool_used AS pool_used, p.root_used AS root_used, p.load5_percent AS load5_percent,
  p.load15_percent AS load15_percent, d.disk_busy AS disk_busy, p.running_vms AS running_vms
FROM pushes AS p LEFT JOIN titles AS t ON t.server = p.resource_id
LEFT JOIN disks AS d ON d.ts = p.ts AND d.resource_id = p.resource_id"""

TREND_READINGS = [
	("CPU Busy", "cpu_busy"),
	("RAM Used", "ram_used"),
	("Storage Pool Used", "pool_used"),
	("Root FS Used", "root_used"),
	("Sys Load 5m", "load5_percent"),
	("Sys Load 15m", "load15_percent"),
	("Disk Busy", "disk_busy"),
	("Running VMs", "running_vms"),
]
# VM counts at each host's newest push, so a state that drops to zero reads zero.
VMS_NOW = f"""WITH {TITLES},
pushes AS (
  SELECT resource_id, max(ts) AS last_ts
  FROM samples
  WHERE metric = 'metal_vm_pending' AND ts > now() - INTERVAL 15 MINUTE AND resource_id IN ({METAL_HOST_IDS})
  GROUP BY resource_id
),
counts AS (
  SELECT s.resource_id AS resource_id,
    sumIf(s.value, s.metric = 'metal_vm_count' AND s.labels['state'] != 'destroyed') AS total,
    sumIf(s.value, s.metric = 'metal_vm_count' AND s.labels['state'] = 'running') AS running,
    sumIf(s.value, s.metric = 'metal_vm_count' AND s.labels['state'] = 'stopped') AS stopped,
    sumIf(s.value, s.metric = 'metal_vm_count' AND s.labels['state'] = 'failed') AS failed,
    sumIf(s.value, s.metric = 'metal_vm_sleepy_count' AND s.labels['state'] != 'destroyed') AS sleepy,
    sumIf(s.value, s.metric = 'metal_vm_sleepy_count' AND s.labels['state'] = 'running') AS sleepy_awake,
    sumIf(s.value, s.metric = 'metal_vm_sleepy_count' AND s.labels['state'] = 'sleeping') AS sleepy_asleep,
    sumIf(s.value, s.metric = 'metal_vm_sleepy_count' AND s.labels['state'] = 'stopped') AS sleepy_stopped
  FROM samples AS s INNER JOIN pushes AS p ON p.resource_id = s.resource_id AND p.last_ts = s.ts
  WHERE s.metric IN ('metal_vm_count', 'metal_vm_sleepy_count') AND s.ts > now() - INTERVAL 15 MINUTE
  GROUP BY s.resource_id
)
SELECT {HOST.format(alias="c")} AS host, c.total AS total, c.running AS running, c.stopped AS stopped, c.failed AS failed,
  c.sleepy AS sleepy, c.sleepy_awake AS sleepy_awake, c.sleepy_asleep AS sleepy_asleep, c.sleepy_stopped AS sleepy_stopped
FROM counts AS c LEFT JOIN titles AS t ON t.server = c.resource_id"""

VM_READINGS = [
	("VMs", "total"),
	("Running", "running"),
	("Stopped", "stopped"),
	("Failed", "failed"),
	("Sleepy VMs", "sleepy"),
	("Sleepy Awake", "sleepy_awake"),
	("Sleepy Asleep", "sleepy_asleep"),
	("Sleepy Stopped", "sleepy_stopped"),
]
QUICK_READINGS = [
	"Pool Size",
	"RAM Total",
	"CPU Cores",
	"Uptime",
	"CPU Temp",
	"Memory Temp",
	"Failed Units",
	"Unhealthy Disks",
]

RECONCILERS_NOW = f"""WITH {TITLES}
SELECT {HOST.format(alias="s")} AS host, s.labels['loop'] AS loop,
  dateDiff('second', toDateTime(argMax(s.value, s.ts)), now()) AS since_last_pass_seconds
FROM samples AS s LEFT JOIN titles AS t ON t.server = s.resource_id
WHERE s.metric = 'metal_reconciler_last_pass_timestamp_seconds' AND s.ts > now() - INTERVAL 15 MINUTE
  AND s.resource_id IN ({METAL_HOST_IDS})
GROUP BY host, loop"""
