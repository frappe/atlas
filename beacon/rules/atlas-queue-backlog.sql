-- title: Atlas queue backlog
-- severity: High
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/atlas-queue-backlog.md
-- interval: 60
-- pending_for: 600
-- resolve_after: 300
SELECT 'atlas' AS resource_id, labels['queue'] AS alert_key, min(value) AS value,
       concat(toString(value), ' or more jobs waited in the ', alert_key, ' queue for 10 min') AS detail
FROM datum.samples_recent
WHERE metric = 'atlas_job_queue_length' AND ts > now() - INTERVAL 10 MINUTE
GROUP BY alert_key
HAVING value >= 100 AND count() >= 5
