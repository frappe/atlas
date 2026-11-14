-- title: Repeated errors
-- severity: Low
-- resource_type: Machine
-- interval: 300
-- pending_for: 300
-- resolve_after: 3600
-- metald logs one line per failed attempt, so one message 10 times in 1 h is a real problem, not a retry. It covers hosts and tenant-0 VMs, such as service nodes and Cargo machines. nginx wraps each message in its time, worker, connection and request, so the query strips them. An upstream that refuses, resets or times out is one site, which the site and proxy checks cover, so those proxy errors do not count.
SELECT resource_id, concat(service, ':', toString(cityHash64(message))) AS alert_key, toFloat64(count()) AS value,
       concat(toString(count()), ' times in 1 h: ', service, ': ', left(message, 100)) AS detail
FROM (
    SELECT resource_id, service,
           replaceRegexpAll(message, '^[0-9/]+ [0-9:]+ .[a-z]+. [0-9#]+: ([*][0-9]+ )?|, client: .*$', '') AS message
    FROM datum.logs
    WHERE ts > now() - INTERVAL 1 HOUR AND level IN ('error', 'critical', 'alert', 'emergency') AND source != 'error_log'
      AND NOT match(message, 'while (connecting to|reading response header from|sending request to) upstream|no live upstreams|upstream prematurely closed')
      AND resource_id IN (
          SELECT labels['machine'] FROM datum.samples_recent
          WHERE metric = 'atlas_machine' AND resource_id = 'atlas' AND labels['kind'] IN ('host', 'service') AND ts > now() - INTERVAL 1 HOUR
      )
)
GROUP BY resource_id, service, message
HAVING value >= 10
