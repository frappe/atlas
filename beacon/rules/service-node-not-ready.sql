-- title: Service node not ready
-- severity: High
-- resource_type: Server
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/service-node-not-ready.md
-- interval: 60
-- pending_for: 180
-- resolve_after: 120
SELECT labels['node'] AS resource_id, labels['service'] AS alert_key, max(value) AS value,
       concat(alert_key, ' on ', resource_id, ' is not ready') AS detail
FROM datum.samples_recent
WHERE metric = 'atlas_service_ready' AND ts > now() - INTERVAL 3 MINUTE
GROUP BY resource_id, alert_key
HAVING value = 0 AND count() >= 2
