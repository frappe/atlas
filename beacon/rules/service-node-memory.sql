-- title: Service node out of memory
-- severity: Medium
-- resource_type: Server
-- interval: 300
-- pending_for: 600
-- resolve_after: 900
SELECT resource_id, 'memory' AS alert_key, used AS value, concat('memory ', toString(round(used)), '% used') AS detail
FROM (
    SELECT resource_id,
           (1 - argMaxIf(value, ts, metric = 'service_memory_available_bytes')
               / nullIf(argMaxIf(value, ts, metric = 'service_memory_total_bytes'), 0)) * 100 AS used
    FROM datum.samples_recent
    WHERE metric IN ('service_memory_available_bytes', 'service_memory_total_bytes') AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY resource_id
    HAVING countIf(metric = 'service_memory_available_bytes') > 0
)
WHERE used > 90
UNION ALL
SELECT resource_id, 'oom-kills', sum(increase), concat(toString(sum(increase)), ' processes killed for memory in 1 h')
FROM datum.counter_increase(metric = 'service_memory_oom_kill_events_total', window = 3600)
GROUP BY resource_id
HAVING sum(increase) > 0
