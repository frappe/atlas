-- title: Host under stress
-- severity: Medium
-- resource_type: Host
-- interval: 300
-- pending_for: 900
-- resolve_after: 900
-- Pressure is the share of 15 minutes that some task waited for CPU, memory or I/O. Memory allocation over 90% is Low: placement treats memory as a hard limit, so the host is only full. CPU can be oversubscribed by design, so CPU allocation does not alert.
SELECT resource_id, alert_key, max(value) AS value, any(detail) AS detail, any(severity) AS severity
FROM (
    SELECT resource_id, concat(labels['resource'], '-pressure') AS alert_key, increase / 9 AS value,
           concat(labels['resource'], ' pressure ', toString(round(value)), '%') AS detail, 'Medium' AS severity
    FROM datum.counter_increase(metric = 'system_pressure_stall_seconds_total', window = 900)
    WHERE labels['kind'] = 'some'
      AND increase / 9 > multiIf(labels['resource'] = 'memory', 5, 10)
    UNION ALL
    SELECT resource_id, 'ram', avg(value), concat('RAM ', toString(round(avg(value))), '% used'), 'Medium'
    FROM datum.samples_recent
    WHERE metric = 'system_memory_usage_percent' AND ts > now() - INTERVAL 15 MINUTE
    GROUP BY resource_id
    HAVING avg(value) > 90
    UNION ALL
    SELECT resource_id, 'load', load_per_core, concat('load ', toString(round(load_per_core, 1)), ' per core'), 'Medium'
    FROM (
        SELECT resource_id,
               avgIf(value, metric = 'system_load_average' AND labels['window'] = '5m')
                   / greatest(maxIf(value, metric = 'system_cpu_count'), 1) AS load_per_core
        FROM datum.samples_recent
        WHERE metric IN ('system_load_average', 'system_cpu_count') AND ts > now() - INTERVAL 15 MINUTE
        GROUP BY resource_id
    )
    WHERE load_per_core > 1.5
    UNION ALL
    SELECT server, 'memory-allocated', allocated, concat('memory ', toString(round(allocated)), '% allocated'), 'Low'
    FROM (
        SELECT labels['server'] AS server,
               (1 - argMaxIf(value, ts, labels['kind'] = 'available') / nullIf(argMaxIf(value, ts, labels['kind'] = 'total'), 0)) * 100 AS allocated
        FROM datum.samples_recent
        WHERE metric = 'atlas_host_memory_bytes' AND ts > now() - INTERVAL 15 MINUTE
        GROUP BY server
    )
    WHERE allocated > 90
)
GROUP BY resource_id, alert_key
