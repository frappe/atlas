-- title: Runway ownership renewal overdue
-- description: The runner pool's last successful ownership claim or renewal is at least 60 seconds old.
-- severity: High
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/runway.md
-- charts: runway-controller-ages
-- interval: 300
-- pending_for: 300
-- resolve_after: 600
-- Keep checking a stopped controller for one hour. A replacement controller's renewal supersedes the old one.
SELECT 'atlas' AS resource_id, pool_key AS alert_key, age AS value,
       concat('last ownership renewal for ', pool_key, ' was ', toString(round(age)), ' s ago') AS detail
FROM (
    SELECT concat(labels['region'], '/', labels['pool']) AS pool_key, toUnixTimestamp(now()) - max(value) AS age
    FROM datum.samples_recent
    WHERE metric = 'runway_controller_ownership_renewal_timestamp_seconds'
      AND ts > now() - INTERVAL 1 HOUR
    GROUP BY pool_key
)
WHERE age >= 60
