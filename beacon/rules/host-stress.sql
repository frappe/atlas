-- title: Host is overloaded
-- description: Tasks on the host wait for CPU, memory or I/O. The host is overloaded.
-- severity: Medium
-- resource_type: Metal Host
-- charts: host-memory, host-pressure
-- interval: 300
-- pending_for: 900
-- resolve_after: 900
-- Pressure is the share of 15 minutes that some task waited for CPU, memory or I/O. Memory allocation over 90% is Low: placement treats memory as a hard limit, so the host is only full. CPU can be oversubscribed by design, so CPU allocation does not alert.
-- machine-stress covers the memory use and the load of every machine, hosts included.
SELECT resource_id, alert_key, max(value) AS value, any(detail) AS detail, any(severity) AS severity
FROM (
    SELECT resource_id, concat(labels['resource'], '-pressure') AS alert_key, increase / 9 AS value,
           concat(labels['resource'], ' pressure ', toString(round(value)), '% (limit ', if(labels['resource'] = 'memory', '5%', '10%'), ')') AS detail, 'Medium' AS severity
    FROM datum.counter_increase(metric = 'system_pressure_stall_seconds_total', window = 900)
    WHERE labels['kind'] = 'some'
      AND increase / 9 > multiIf(labels['resource'] = 'memory', 5, 10)
    UNION ALL
    SELECT server, 'memory-allocated', allocated, concat('memory ', toString(round(allocated)), '% allocated (limit 90%)'), 'Low'
    FROM (
        SELECT labels['server'] AS server,
               (1 - argMaxIf(value, ts, labels['kind'] = 'available') / nullIf(argMaxIf(value, ts, labels['kind'] = 'total'), 0)) * 100 AS allocated
        FROM datum.samples_recent
        WHERE metric = 'atlas_host_memory_bytes' AND resource_id = 'atlas' AND ts > now() - INTERVAL 15 MINUTE
        GROUP BY server
    )
    WHERE allocated > 90
)
GROUP BY resource_id, alert_key
