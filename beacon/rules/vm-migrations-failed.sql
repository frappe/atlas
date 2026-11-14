-- title: VM migrations failed
-- severity: Medium
-- resource_type: Host
-- interval: 300
-- pending_for: 300
-- resolve_after: 3600
SELECT resource_id, attributes['virtual_machine_id'] AS alert_key, toFloat64(count()) AS value,
       concat('migration of ', alert_key, ' failed: ', left(any(attributes['error']), 100)) AS detail
FROM datum.logs
WHERE message = 'migration state changed' AND attributes['to'] = 'failed' AND ts > now() - INTERVAL 1 HOUR
GROUP BY resource_id, alert_key
