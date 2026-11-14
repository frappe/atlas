-- title: Service nodes failed
-- severity: High
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/atlas-service-nodes-failed.md
-- interval: 60
-- pending_for: 120
-- resolve_after: 300
-- A Failed node has no readiness sample, so Service node not ready cannot see it. Only the latest Atlas collection counts,
-- because Atlas sends no sample for a status that no node has.
SELECT 'atlas' AS resource_id, labels['service'] AS alert_key, sum(value) AS value,
       concat(toString(value), ' ', alert_key, ' nodes are Failed') AS detail
FROM datum.samples_recent
WHERE metric = 'atlas_service_servers' AND labels['status'] = 'Failed'
  AND ts = (SELECT max(ts) FROM datum.samples_recent WHERE metric = 'atlas_service_servers' AND ts > now() - INTERVAL 10 MINUTE)
GROUP BY alert_key
HAVING value > 0
