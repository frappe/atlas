-- title: Servers
-- description: Servers that should run are available.
-- scope: Region
-- slo_target: 99.9
-- sort_order: 2
SELECT w.resource_id AS resource_id,
       toInt64(greatest(w.wanted - u.unavailable, 0)) AS good,
       toInt64(w.wanted) AS valid,
       w.last_ts AS last_ts
FROM (
    SELECT resource_id, argMax(value, ts) AS wanted, max(ts) AS last_ts
    FROM datum.samples_recent
    WHERE metric = 'metal_vm_wanted_running' AND ts > now() - INTERVAL 30 MINUTE
    GROUP BY resource_id
) AS w
LEFT JOIN (
    SELECT resource_id, sum(value) AS unavailable
    FROM datum.latest_value(metric = 'metal_vm_unavailable', window = 1800)
    GROUP BY resource_id
) AS u ON u.resource_id = w.resource_id
