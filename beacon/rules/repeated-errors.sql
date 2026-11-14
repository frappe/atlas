-- title: Repeated errors
-- severity: Low
-- resource_type: Machine
-- interval: 300
-- pending_for: 300
-- resolve_after: 3600
-- metald logs one line per failed attempt, so one message 10 times in 1 h is a real problem, not a retry. It covers hosts and tenant-0 VMs, such as service nodes and Cargo machines. nginx wraps each message in its time, worker, connection and request, so the query strips them. The Datum agent does not send nginx upstream errors, because the site and proxy checks cover them.
SELECT resource_id, concat(service, ':', toString(cityHash64(message))) AS alert_key, toFloat64(count()) AS value,
       concat(toString(count()), ' times in 1 h: ', service, ': ', left(message, 100)) AS detail
FROM (
    SELECT resource_id, service,
           replaceRegexpAll(message, '^[0-9/]+ [0-9:]+ .[a-z]+. [0-9#]+: ([*][0-9]+ )?|, client: .*$', '') AS message
    FROM datum.logs
    WHERE ts > now() - INTERVAL 1 HOUR AND level IN ('error', 'critical', 'alert', 'emergency') AND source != 'error_log'
      AND resource_id IN (
          SELECT labels['machine'] FROM datum.samples_recent
          WHERE metric = 'atlas_machine' AND resource_id = 'atlas' AND labels['kind'] IN ('host', 'service') AND ts > now() - INTERVAL 1 HOUR
      )
)
GROUP BY resource_id, service, message
HAVING value >= 10
