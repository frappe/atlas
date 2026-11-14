-- title: Atlas jobs failing
-- severity: Medium
-- resource_type: Atlas
-- interval: 60
-- pending_for: 60
-- resolve_after: 900
-- The failed job registry only grows until RQ expires old entries, so growth in 15 minutes counts new failures.
SELECT 'atlas' AS resource_id, labels['queue'] AS alert_key,
       max(value) - argMin(value, ts) AS value,
       concat(toString(value), ' jobs failed in the ', alert_key, ' queue in 15 min') AS detail
FROM datum.samples_recent
WHERE metric = 'atlas_job_failed' AND ts > now() - INTERVAL 15 MINUTE
GROUP BY alert_key
HAVING value >= 5
