-- title: Runway waiting demand without progress
-- description: The runner pool has waiting demand and no executing job for at least five minutes.
-- severity: High
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/runway.md
-- charts: runway-controller-ages
-- interval: 300
-- pending_for: 300
-- resolve_after: 600
SELECT 'atlas' AS resource_id, pool_key AS alert_key, age AS value,
       concat(pool_key, ' has waited ', toString(round(age)), ' s without progress') AS detail
FROM (
    SELECT concat(labels['region'], '/', labels['pool']) AS pool_key, argMax(value, ts) AS age
    FROM datum.samples_recent
    WHERE metric = 'runway_controller_no_progress_seconds' AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY pool_key
)
WHERE age >= 300
