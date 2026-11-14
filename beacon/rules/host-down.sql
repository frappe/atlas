-- title: Host down
-- severity: Critical
-- resource_type: Host
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/host-down.md
-- interval: 60
-- pending_for: 60
-- resolve_after: 120
-- scope_includes_descendants: 1
-- Start from the Running hosts that Atlas lists, so a host whose sync series stops stays in alert.
SELECT h.server AS resource_id, '' AS alert_key, ifNull(s.age, -1) AS value,
       concat(if(value < 0, 'no host sync reported', concat('no host sync for ', toString(round(value)), ' s')),
              ', metald silent') AS detail
FROM (
    SELECT labels['server'] AS server, argMax(labels['status'], ts) AS status
    FROM datum.samples_recent
    WHERE metric = 'atlas_metal_server_info' AND ts > now() - INTERVAL 10 MINUTE
    GROUP BY server
) AS h
LEFT JOIN (
    SELECT labels['server'] AS server, argMax(value, ts) AS age
    FROM datum.samples_recent
    WHERE metric = 'atlas_host_sync_age_seconds' AND ts > now() - INTERVAL 10 MINUTE
    GROUP BY server
) AS s ON s.server = h.server
LEFT JOIN (
    SELECT resource_id, max(ts) AS last_ts
    FROM datum.samples_recent
    WHERE metric = 'metal_build_info' AND ts > now() - INTERVAL 15 MINUTE
    GROUP BY resource_id
) AS m ON m.resource_id = h.server
WHERE h.status = 'Running' AND (s.age IS NULL OR s.age > 300) AND ifNull(m.last_ts, toDateTime64(0, 3)) < now() - INTERVAL 5 MINUTE
SETTINGS join_use_nulls = 1
