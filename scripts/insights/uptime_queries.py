from workbook import UPTIME_PERCENT, dimension, measure

MONTH_START = "toDateTime(toStartOfMonth(now()))"
DAYS = "toDateTime(today() - 27)"

# VM uptime, the SLA. Each minute, a host that reports counts its unavailable VMs.
# A host that stops reporting counts every VM it last reported as wanted running,
# because its VMs are down with it. Minutes before a host's first report do not count,
# and neither does a minute in which no host reported: that is a gap in telemetry.
VM_UPTIME_TARGET = 0.999


def vm_downtime_minutes_sql(start: str) -> str:
	"""One row per host and minute since start: VMs wanted running and VMs down."""
	return f"""WITH
servers AS (
  SELECT DISTINCT labels['server'] AS server FROM samples
  WHERE resource_id LIKE 'atlas-%' AND metric = 'atlas_metal_server_info' AND labels['status'] = 'Running' AND ts > {start}
),
pushes AS (
  SELECT toStartOfMinute(ts) AS minute, resource_id,
    maxIf(value, metric = 'metal_vm_wanted_running') AS wanted, sumIf(value, metric = 'metal_vm_unavailable') AS down
  FROM samples
  WHERE metric IN ('metal_vm_wanted_running', 'metal_vm_unavailable') AND ts > {start}
  GROUP BY minute, resource_id, ts
),
reported AS (
  SELECT minute, resource_id, max(wanted) AS wanted, max(down) AS down FROM pushes GROUP BY minute, resource_id
),
grid AS (
  SELECT s.server AS server, m.minute AS minute
  FROM servers AS s
  CROSS JOIN (
    SELECT toStartOfMinute(now()) - toIntervalMinute(number + 1) AS minute
    FROM numbers(toUInt64(dateDiff('minute', {start}, now())))
  ) AS m
)
SELECT minute, server, wanted, down FROM (
  SELECT g.minute AS minute, g.server AS server, r.resource_id != '' AS reported,
    if(reported, r.wanted, NULL) AS reported_wanted, r.down AS reported_down,
    anyLast(reported_wanted) OVER (PARTITION BY g.server ORDER BY g.minute ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS last_wanted,
    if(reported, r.wanted, last_wanted) AS wanted,
    if(reported, r.down, last_wanted) AS down
  FROM grid AS g LEFT JOIN reported AS r ON r.resource_id = g.server AND r.minute = g.minute
)
WHERE wanted IS NOT NULL AND minute IN (SELECT minute FROM reported)"""


# Each VM's downtime from metald's log this month, and the seconds since its host
# first reported this month and its newest push.
VM_DOWNTIME_THIS_MONTH = f"""measured AS (
  SELECT resource_id, dateDiff('second', min(ts), now()) AS seconds, max(ts) AS last_push
  FROM samples WHERE metric = 'metal_vm_wanted_running' AND ts > {MONTH_START}
  GROUP BY resource_id
),
downtime AS (
  SELECT attributes['virtual_machine_id'] AS vm, argMax(resource_id, ts) AS resource_id,
    sumIf(toInt64OrZero(attributes['duration']), message = 'virtual machine available again') / 1e9
      + if(argMax(message, ts) = 'virtual machine unavailable', dateDiff('second', max(ts), now()), 0) AS seconds,
    argMax(attributes['reason'], ts) AS last_reason, argMax(message, ts) = 'virtual machine unavailable' AS is_down_now
  FROM logs
  WHERE message IN ('virtual machine unavailable', 'virtual machine available again') AND ts > {MONTH_START}
  GROUP BY vm
)"""


# Insights starts a value axis at 0, which flattens uptime near 100%. A bad day dives below the axis.
UPTIME_AXIS = {"min": 90, "max": 100, "smooth": True, "show_data_points": True, "show_data_labels": True}


def daily_lines(value_name: str) -> dict:
	"""One colored line per host, one point per day."""
	return {
		"x_axis": {"dimension": dimension("day", "Date", granularity="day")},
		"y_axis": {"series": [{"measure": measure(value_name, "value", "min")}], **UPTIME_AXIS},
		"split_by": {"dimension": dimension("series"), "max_split_values": 20},
		"number_formats": {value_name: UPTIME_PERCENT},
		"limit": 10000,
	}
