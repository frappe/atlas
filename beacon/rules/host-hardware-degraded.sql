-- title: Host hardware degraded
-- severity: High
-- resource_type: Host
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/host-hardware-degraded.md
-- interval: 300
-- pending_for: 300
-- resolve_after: 3600
-- An uncorrected memory error pages. A failing disk and a CPU over 85 °C for 15 min are Medium. Many corrected memory errors, CPU throttling and slow ZFS I/O are Low: the part wears out.
SELECT resource_id, alert_key, max(value) AS value, any(detail) AS detail, any(severity) AS severity
FROM (
    SELECT resource_id, 'ecc-uncorrected' AS alert_key, increase AS value,
           concat(toString(increase), ' uncorrected memory errors in 15 min') AS detail, 'High' AS severity
    FROM datum.counter_increase(metric = 'system_edac_uncorrectable_errors_total', window = 900)
    WHERE labels['csrow'] = '' AND increase > 0
    UNION ALL
    SELECT resource_id, 'ecc-corrected', increase, concat(toString(increase), ' corrected memory errors in 1 h'), 'Low'
    FROM datum.counter_increase(metric = 'system_edac_correctable_errors_total', window = 3600)
    WHERE labels['csrow'] = '' AND increase >= 10
    UNION ALL
    SELECT resource_id, 'cpu-throttle', sum(increase), concat(toString(sum(increase)), ' CPU throttles in 1 h'), 'Low'
    FROM datum.counter_increase(metric = 'system_cpu_core_throttles_total', window = 3600)
    GROUP BY resource_id
    HAVING sum(increase) >= 100
    UNION ALL
    SELECT resource_id, 'cpu-temperature', max(coolest), concat('CPU at ', toString(round(max(coolest))), ' °C or more for 15 min'), 'Medium'
    FROM (
        SELECT resource_id, labels, min(value) AS coolest FROM datum.samples_recent
        WHERE metric = 'system_hwmon_temperature_celsius' AND labels['chip'] = 'coretemp' AND labels['attribute'] = 'input'
          AND ts > now() - INTERVAL 15 MINUTE
        GROUP BY resource_id, labels
    )
    GROUP BY resource_id
    HAVING max(coolest) > 85
    UNION ALL
    SELECT resource_id, concat('zfs-slow-io:', labels['device']), increase,
           concat(toString(increase), ' slow ZFS I/Os on ', labels['device'], ' in 1 h'), 'Low'
    FROM datum.counter_increase(metric = 'system_zfs_device_slow_ios', window = 3600)
    WHERE increase >= 10
    UNION ALL
    -- A disk is unhealthy when SMART says so, has pending sectors (197) or NVMe critical warnings, or reallocated sectors (5) or NVMe media errors grew in 1 day.
    SELECT resource_id, concat('disk:', device), 1, concat('disk ', device, ' is failing'), 'Medium'
    FROM (
        SELECT resource_id, labels['device'] AS device,
               coalesce(minIfOrNull(value, metric = 'system_smart_healthy' AND ts > now() - INTERVAL 1 HOUR), 1) = 0
               OR maxIf(value, metric = 'system_smart_attribute' AND labels['field'] = 'raw' AND labels['id'] = '197' AND ts > now() - INTERVAL 1 HOUR) > 0
               OR maxIf(value, metric = 'system_nvme_smart' AND labels['field'] = 'critical_warning' AND ts > now() - INTERVAL 1 HOUR) > 0
               OR maxIf(value, metric = 'system_smart_attribute' AND labels['field'] = 'raw' AND labels['id'] = '5')
                  > minIf(value, metric = 'system_smart_attribute' AND labels['field'] = 'raw' AND labels['id'] = '5')
               OR maxIf(value, metric = 'system_nvme_smart' AND labels['field'] = 'media_errors')
                  > minIf(value, metric = 'system_nvme_smart' AND labels['field'] = 'media_errors') AS is_unhealthy
        FROM datum.samples_recent
        WHERE metric IN ('system_smart_healthy', 'system_smart_attribute', 'system_nvme_smart') AND ts > now() - INTERVAL 1 DAY
        GROUP BY resource_id, device
    )
    WHERE is_unhealthy
)
GROUP BY resource_id, alert_key
