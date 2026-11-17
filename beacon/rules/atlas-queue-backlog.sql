-- title: Atlas job queue is backed up
-- description: Jobs wait too long in an Atlas queue. Provisioning, VM actions and syncs are late.
-- severity: High
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/atlas-queue-backlog.md
-- interval: 60
-- pending_for: 600
-- resolve_after: 300
SELECT 'atlas' AS resource_id, queue AS alert_key, shortest AS value,
       concat('queue ', queue, ' held at least ', toString(shortest), ' jobs for 10 min') AS detail
FROM (
    SELECT labels['queue'] AS queue, min(value) AS shortest, count() AS samples
    FROM datum.samples_recent
    WHERE metric = 'atlas_job_queue_length' AND resource_id = 'atlas' AND ts > now() - INTERVAL 10 MINUTE
    GROUP BY queue
)
WHERE shortest >= 100 AND samples >= 5
