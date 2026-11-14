-- title: Host systemd units failing
-- severity: Medium
-- resource_type: Host
-- interval: 300
-- pending_for: 300
-- resolve_after: 3600
-- A failed unit is Medium. A unit that restarts 3 times in 1 h is Low: it recovers, but something kills it.
-- metald sends only the current state of each unit, so the newest state decides. An old failed series stays in the window after recovery.
SELECT resource_id, concat('failed:', unit) AS alert_key, 1 AS value, concat(unit, ' failed') AS detail, 'Medium' AS severity
FROM (
    SELECT resource_id, labels['unit'] AS unit, argMax(labels['state'], ts) AS state
    FROM datum.samples_recent
    WHERE metric = 'system_systemd_unit_state' AND value > 0 AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY resource_id, unit
)
WHERE state = 'failed'
UNION ALL
SELECT resource_id, concat('restarts:', labels['unit']), sum(increase),
       concat(labels['unit'], ' restarted ', toString(sum(increase)), ' times in 1 h'), 'Low'
FROM datum.counter_increase(metric = 'system_systemd_unit_restarts_total', window = 3600)
GROUP BY resource_id, labels['unit']
HAVING sum(increase) >= 3
