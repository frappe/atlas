-- title: Machine under stress
-- severity: Medium
-- resource_type: Machine
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/machine-stress.md
-- interval: 300
-- pending_for: 600
-- resolve_after: 900
-- Over 90% of the memory in use, or a 5-minute load over 1.5 for each core, for 15 minutes.
SELECT resource_id, 'memory' AS alert_key, used AS value, concat('memory ', toString(round(used)), '% used') AS detail
FROM (
    SELECT resource_id,
           (1 - avgIf(value, metric = 'system_memory_available_bytes') / nullIf(avgIf(value, metric = 'system_memory_total_bytes'), 0)) * 100 AS used
    FROM datum.samples_recent
    WHERE metric IN ('system_memory_available_bytes', 'system_memory_total_bytes') AND ts > now() - INTERVAL 15 MINUTE
    GROUP BY resource_id
    HAVING countIf(metric = 'system_memory_available_bytes') > 0
)
WHERE used > 90
UNION ALL
SELECT resource_id, 'load', load_per_core, concat('load ', toString(round(load_per_core, 1)), ' per core')
FROM (
    SELECT resource_id,
           avgIf(value, metric = 'system_load_average' AND labels['window'] = '5m')
               / greatest(maxIf(value, metric = 'system_cpu_count'), 1) AS load_per_core
    FROM datum.samples_recent
    WHERE metric IN ('system_load_average', 'system_cpu_count') AND ts > now() - INTERVAL 15 MINUTE
    GROUP BY resource_id
)
WHERE load_per_core > 1.5
