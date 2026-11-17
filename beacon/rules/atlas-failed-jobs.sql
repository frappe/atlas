-- title: Atlas background jobs failing
-- description: Background jobs in Atlas keep failing. Provisioning, VM actions and syncs are late.
-- severity: Medium
-- resource_type: Atlas
-- charts: atlas-jobs
-- interval: 60
-- pending_for: 60
-- resolve_after: 900
-- The failed job registry only grows until RQ expires old entries, so growth in 1 h counts new failures. 3 failures in 1 h is a job that keeps failing, not one bad run.
-- affected names the methods that failed in the last hour, from atlas_jobs_failed_last_hour.
SELECT 'atlas' AS resource_id, f.queue AS alert_key, failed AS value,
       concat(toString(failed), ' jobs failed in the ', f.queue, ' queue in 1 h (limit 3)') AS detail,
       m.affected AS affected
FROM (
    SELECT labels['queue'] AS queue, max(value) - argMin(value, ts) AS failed
    FROM datum.samples_recent
    WHERE metric = 'atlas_job_failed' AND resource_id = 'atlas' AND ts > now() - INTERVAL 1 HOUR
    GROUP BY queue
) AS f
LEFT JOIN (
    SELECT labels['queue'] AS queue,
           mapFromArrays(groupArray(50)(labels['method']), groupArray(50)(concat(toString(value), ' failed in 1 h'))) AS affected
    FROM datum.latest_value(metric = 'atlas_jobs_failed_last_hour', window = 300)
    WHERE resource_id = 'atlas'
    GROUP BY queue
) AS m ON m.queue = f.queue
WHERE failed >= 3
