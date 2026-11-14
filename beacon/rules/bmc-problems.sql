-- title: BMC problems
-- severity: High
-- resource_type: Host
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/bmc-problems.md
-- interval: 300
-- pending_for: 300
-- resolve_after: 900
-- Atlas reads each BMC every 5 minutes. A critical BMC event pages. An unhealthy component and an unreachable or unread BMC are Medium. A warning event is Low.
SELECT resource_id, alert_key, max(value) AS value, any(detail) AS detail, any(severity) AS severity
FROM (
    SELECT resource_id, concat(level, '-events') AS alert_key, toFloat64(count()) AS value,
           concat(toString(count()), ' ', level, ' BMC events: ', arrayStringConcat(arraySlice(arraySort(groupUniqArray(
               if(attributes['sensor_type'] = '', left(message, 80), concat(attributes['sensor_type'], ': ', left(message, 60))))), 1, 3), '; ')) AS detail,
           if(level = 'critical', 'High', 'Low') AS severity
    FROM datum.logs
    WHERE source = 'bmc' AND ((level = 'critical' AND ts > now() - INTERVAL 15 MINUTE) OR (level = 'warning' AND ts > now() - INTERVAL 1 HOUR))
    GROUP BY resource_id, level
    UNION ALL
    SELECT resource_id, 'unreachable', toFloat64(0), 'the BMC does not answer', 'Medium'
    FROM datum.latest_value(metric = 'bmc_up', window = 1800)
    WHERE value = 0
    UNION ALL
    SELECT resource_id, 'unhealthy-component', toFloat64(count()), concat(toString(count()), ' BMC components report a fault'), 'Medium'
    FROM (
        SELECT resource_id, value FROM datum.latest_value(metric = 'bmc_health', window = 900)
        UNION ALL
        SELECT resource_id, value FROM datum.latest_value(metric = 'bmc_component_health', window = 900)
    )
    WHERE value > 0
    GROUP BY resource_id
    UNION ALL
    -- A host that Atlas lists with a BMC must have a BMC reading from the last 15 minutes.
    SELECT h.server, 'not-read', toFloat64(ifNull(dateDiff('second', r.last_ts, now()), -1)), 'Atlas did not read the BMC for 15 min', 'Medium'
    FROM (
        SELECT labels['server'] AS server FROM datum.samples_recent
        WHERE metric = 'atlas_metal_server_info' AND ts > now() - INTERVAL 15 MINUTE
        GROUP BY server
        HAVING argMax(labels['bmc'], ts) = 'true' AND argMax(labels['status'], ts) = 'Running'
    ) AS h
    LEFT JOIN (
        SELECT resource_id, max(ts) AS last_ts FROM datum.samples_recent
        WHERE metric = 'bmc_up' AND ts > now() - INTERVAL 1 DAY
        GROUP BY resource_id
    ) AS r ON r.resource_id = h.server
    WHERE r.last_ts IS NULL OR r.last_ts < now() - INTERVAL 15 MINUTE
    SETTINGS join_use_nulls = 1
)
GROUP BY resource_id, alert_key
