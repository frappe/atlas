-- title: Service node silent
-- severity: High
-- resource_type: Server
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/service-node-silent.md
-- interval: 60
-- pending_for: 120
-- resolve_after: 120
-- Start from the nodes that Atlas runs, so a node stays in alert however long it is silent. Vector on each node sends service_load5. No sample in 1 hour reads as silent.
-- An IPv6 router has no readiness probe, so its silence pages. Other nodes are Medium, because service-node-not-ready pages when the node itself stops.
SELECT n.node AS resource_id, '' AS alert_key, toFloat64(if(l.last_ts IS NULL, 3600, dateDiff('second', l.last_ts, now()))) AS value,
       if(l.last_ts IS NULL, 'no telemetry for over 1 h', concat('no telemetry for ', toString(round(value / 60)), ' min')) AS detail,
       if(n.service = 'ipv6_router', 'High', 'Medium') AS severity
FROM (
    SELECT labels['node'] AS node, any(labels['service']) AS service
    FROM datum.samples_recent
    WHERE metric = 'atlas_service_node' AND labels['service'] != 'cargo'
      AND ts > (SELECT max(ts) FROM datum.samples_recent WHERE metric = 'atlas_service_node' AND ts > now() - INTERVAL 15 MINUTE) - INTERVAL 2 MINUTE
    GROUP BY node
) AS n
LEFT JOIN (
    SELECT resource_id, max(ts) AS last_ts
    FROM datum.samples_recent
    WHERE metric = 'service_load5' AND ts > now() - INTERVAL 1 HOUR
      AND resource_id IN (SELECT labels['node'] FROM datum.samples_recent WHERE metric = 'atlas_service_node' AND ts > now() - INTERVAL 15 MINUTE)
    GROUP BY resource_id
) AS l ON l.resource_id = n.node
WHERE l.last_ts IS NULL OR l.last_ts < now() - INTERVAL 3 MINUTE
SETTINGS join_use_nulls = 1
