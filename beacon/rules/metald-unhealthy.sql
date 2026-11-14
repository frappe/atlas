-- title: metald unhealthy
-- severity: High
-- resource_type: Host
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/metald-unhealthy.md
-- interval: 60
-- pending_for: 120
-- resolve_after: 300
-- A stalled loop, an old pending change or failing VMs mean metald does not apply VM changes. The image loop runs each hour. Dropped telemetry is Low.
SELECT resource_id, concat('stalled:', loop) AS alert_key, age AS value,
       concat('the ', loop, ' loop did not run for ', toString(round(age / 60)), ' min') AS detail,
       'High' AS severity
FROM (
    SELECT resource_id, labels['loop'] AS loop, toUnixTimestamp(now()) - value AS age
    FROM datum.latest_value(metric = 'metal_reconciler_last_pass_timestamp_seconds', window = 900)
)
WHERE age > if(loop = 'image', 7200, 300)
UNION ALL
SELECT resource_id, 'pending-change', value,
       concat('a VM change is waiting for ', toString(round(value / 60)), ' min'), 'High'
FROM datum.latest_value(metric = 'metal_vm_oldest_pending_seconds', window = 300)
WHERE value > 600
UNION ALL
SELECT resource_id, 'failing-vms', value, concat(toString(value), ' VMs have a metald error'), 'High'
FROM datum.latest_value(metric = 'metal_vm_failing', window = 300)
WHERE value > 0
UNION ALL
SELECT resource_id, 'dropped-telemetry', sum(increase),
       concat(toString(sum(increase)), ' telemetry observations or collector errors in 1 h'), 'Low'
FROM (
    SELECT resource_id, increase FROM datum.counter_increase(metric = 'metal_telemetry_observations_dropped_total', window = 3600)
    UNION ALL
    SELECT resource_id, increase FROM datum.counter_increase(metric = 'metal_telemetry_collector_errors_total', window = 3600)
)
GROUP BY resource_id
HAVING sum(increase) > 0
