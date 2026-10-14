from workbook import UPTIME_PERCENT, dimension, measure

MONTH_START = "toDateTime(toStartOfMonth(now()))"
DAYS = "toDateTime(today() - 27)"

# VM uptime, the SLA. Each minute, a host that reports counts its unavailable VMs.
# A host that stops reporting counts every VM it last reported as wanted running,
# because its VMs are down with it. Minutes before a host's first report do not count,
# and neither does a minute in which no host reported: that is a gap in telemetry.
# A host counts only in the minutes in which Atlas reported it as Running.
VM_UPTIME_TARGET = 0.999
# Transitions are read from this far before the month, so an outage that started
# earlier counts from the month start. metald logs a VM that is still down again
# after a restart, so an open outage starts at its first such line.
OUTAGE_LOOKBACK = "INTERVAL 90 DAY"


def vm_downtime_minutes_sql(start: str) -> str:
	"""One row per host and minute since start: VMs wanted running and VMs down."""
	return f"""WITH
running AS (
  SELECT DISTINCT toStartOfMinute(ts) AS minute, labels['server'] AS server FROM samples
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
)
SELECT minute, server, wanted, down FROM (
  SELECT g.minute AS minute, g.server AS server, r.resource_id != '' AS reported,
    if(reported, r.wanted, NULL) AS reported_wanted, r.down AS reported_down,
    anyLast(reported_wanted) OVER (PARTITION BY g.server ORDER BY g.minute ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS last_wanted,
    if(reported, r.wanted, last_wanted) AS wanted,
    if(reported, r.down, last_wanted) AS down
  FROM running AS g LEFT JOIN reported AS r ON r.resource_id = g.server AND r.minute = g.minute
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
    argMax(message, ts) = 'virtual machine unavailable' AS is_down_now, argMax(attributes['reason'], ts) AS last_reason,
    maxIf(ts, message = 'virtual machine available again') AS last_recovered,
    sumIf(least(toInt64OrZero(attributes['duration']) / 1e9, toFloat64(dateDiff('second', {MONTH_START}, ts))),
      message = 'virtual machine available again' AND ts > {MONTH_START})
      + if(is_down_now, dateDiff('second', greatest(arrayMin(arrayFilter((t, m) -> m = 'virtual machine unavailable' AND t > last_recovered, groupArray(ts), groupArray(message))), {MONTH_START}), now()), 0) AS seconds,
    countIf(message = 'virtual machine available again' AND ts > {MONTH_START}) + is_down_now AS periods
  FROM logs
  WHERE message IN ('virtual machine unavailable', 'virtual machine available again') AND ts > {MONTH_START} - {OUTAGE_LOOKBACK}
  GROUP BY vm
  HAVING seconds > 0
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
