-- title: Atlas jobs failing
-- severity: Medium
-- resource_type: Atlas
-- interval: 60
-- pending_for: 60
-- resolve_after: 900
-- The failed job registry only grows until RQ expires old entries, so growth in 1 h counts new failures. 3 failures in 1 h is a job that keeps failing, not one bad run.
SELECT 'atlas' AS resource_id, labels['queue'] AS alert_key,
       max(value) - argMin(value, ts) AS value,
       concat(toString(value), ' jobs failed in the ', alert_key, ' queue in 1 h') AS detail
FROM datum.samples_recent
WHERE metric = 'atlas_job_failed' AND ts > now() - INTERVAL 1 HOUR
GROUP BY alert_key
HAVING value >= 3
