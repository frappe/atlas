-- title: Atlas cannot sync Warpgate
-- description: Atlas cannot sync Warpgate targets with the hosts. New hosts are unreachable through it.
-- severity: Medium
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/warpgate-sync-failing.md
-- charts: atlas-host-sync-age
-- interval: 60
-- pending_for: 300
-- resolve_after: 120
-- Atlas syncs the Warpgate targets of the hosts each minute. Without a full sync for 15 minutes, people cannot reach a new or renamed host through Warpgate.
SELECT 'atlas' AS resource_id, '' AS alert_key, age AS value,
       concat('no full Warpgate sync for ', toString(round(age / 60)), ' min (limit 15 min)') AS detail
FROM (
    SELECT max(value) AS age
    FROM datum.latest_value(metric = 'atlas_warpgate_sync_age_seconds', window = 600)
    WHERE resource_id = 'atlas'
)
WHERE age > 900
