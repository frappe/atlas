-- title: Warpgate sync failing
-- severity: Medium
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/warpgate-sync-failing.md
-- interval: 60
-- pending_for: 300
-- resolve_after: 120
-- Atlas syncs the Warpgate targets of the hosts each minute. Without a full sync for 15 minutes, people cannot reach a new or renamed host through Warpgate.
SELECT 'atlas' AS resource_id, '' AS alert_key, max(value) AS value,
       concat('no full Warpgate sync for ', toString(round(value / 60)), ' min') AS detail
FROM datum.latest_value(metric = 'atlas_warpgate_sync_age_seconds', window = 600)
HAVING value > 900
