-- title: Atlas service nodes have failed
-- description: Nodes of an Atlas service have failed. Traffic can be down without a readiness alert.
-- severity: High
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/atlas-service-nodes-failed.md
-- charts: atlas-service-nodes
-- interval: 60
-- pending_for: 120
-- resolve_after: 300
-- A Failed node has no readiness sample, so Service node not ready cannot see it. Only the latest Atlas collection counts,
-- because Atlas sends no sample for a status that no node has.
SELECT 'atlas' AS resource_id, service AS alert_key, failed AS value,
       concat(toString(failed), ' ', service, ' nodes have failed') AS detail
FROM (
    SELECT labels['service'] AS service, sum(value) AS failed
    FROM datum.samples_recent
    WHERE metric = 'atlas_service_servers' AND resource_id = 'atlas' AND labels['status'] = 'Failed'
      AND ts = (SELECT max(ts) FROM datum.samples_recent
                WHERE metric = 'atlas_service_servers' AND resource_id = 'atlas' AND ts > now() - INTERVAL 10 MINUTE)
    GROUP BY service
)
WHERE failed > 0
