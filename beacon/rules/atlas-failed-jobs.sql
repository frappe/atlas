-- title: Atlas background jobs failing
-- description: Background jobs in Atlas keep failing. Provisioning, VM actions and syncs are late.
-- severity: Medium
-- resource_type: Atlas
-- interval: 60
-- pending_for: 60
-- resolve_after: 900
-- The failed job registry only grows until RQ expires old entries, so growth in 1 h counts new failures. 3 failures in 1 h is a job that keeps failing, not one bad run.
SELECT 'atlas' AS resource_id, queue AS alert_key, failed AS value,
       concat(toString(failed), ' jobs failed in the ', queue, ' queue in 1 h (limit 3)') AS detail
FROM (
    SELECT labels['queue'] AS queue, max(value) - argMin(value, ts) AS failed
    FROM datum.samples_recent
    WHERE metric = 'atlas_job_failed' AND resource_id = 'atlas' AND ts > now() - INTERVAL 1 HOUR
    GROUP BY queue
)
WHERE failed >= 3
