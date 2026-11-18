-- title: Runway lease cleanup overdue
-- description: A lease has been waiting for VM or GitHub cleanup for at least ten minutes.
-- severity: Medium
-- resource_type: Atlas
-- charts: runway-controller-ages
-- interval: 3600
-- pending_for: 3600
-- resolve_after: 7200
SELECT 'atlas' AS resource_id, pool_key AS alert_key, age AS value,
       concat(pool_key, ' has waited ', toString(round(age)), ' s waiting for cleanup') AS detail
FROM (
    SELECT concat(labels['region'], '/', labels['pool']) AS pool_key, argMax(value, ts) AS age
    FROM datum.samples_recent
    WHERE metric = 'runway_controller_cleanup_oldest_seconds' AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY pool_key
)
WHERE age >= 600
