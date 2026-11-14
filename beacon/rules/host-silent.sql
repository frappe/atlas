-- title: Host telemetry silent
-- severity: Medium
-- resource_type: Host
-- interval: 60
-- pending_for: 120
-- resolve_after: 120
-- metald on each Running host sends metal_vm_pending every 30 seconds. Silence for 3 minutes, while Atlas can still sync the host, means the telemetry loop or its token broke: every host check is blind. host-down pages when the sync also stops.
SELECT h.server AS resource_id, '' AS alert_key, toFloat64(if(l.last_ts IS NULL, 3600, dateDiff('second', l.last_ts, now()))) AS value,
       if(l.last_ts IS NULL, 'no metald telemetry for over 1 h', concat('no metald telemetry for ', toString(round(value / 60)), ' min')) AS detail
FROM (
    SELECT labels['server'] AS server
    FROM datum.samples_recent
    WHERE metric = 'atlas_metal_server_info' AND ts > now() - INTERVAL 10 MINUTE
    GROUP BY server
    HAVING argMax(labels['status'], ts) = 'Running'
) AS h
LEFT JOIN (
    SELECT resource_id, max(ts) AS last_ts
    FROM datum.samples_recent
    WHERE metric = 'metal_vm_pending' AND ts > now() - INTERVAL 1 HOUR
      AND resource_id IN (SELECT labels['server'] FROM datum.samples_recent WHERE metric = 'atlas_metal_server_info' AND ts > now() - INTERVAL 10 MINUTE)
    GROUP BY resource_id
) AS l ON l.resource_id = h.server
WHERE l.last_ts IS NULL OR l.last_ts < now() - INTERVAL 3 MINUTE
SETTINGS join_use_nulls = 1
