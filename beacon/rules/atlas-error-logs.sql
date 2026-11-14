-- title: Atlas unhandled errors
-- severity: Medium
-- resource_type: Atlas
-- interval: 60
-- pending_for: 60
-- resolve_after: 900
-- One alert for each error fingerprint that repeats. Errors that Atlas logs on purpose are handled and not counted.
SELECT 'atlas' AS resource_id, attributes['fingerprint'] AS alert_key, count() AS value,
       concat(toString(value), ' x ', any(message), ' in 15 min') AS detail
FROM datum.logs
WHERE product = 'atlas' AND source = 'error_log' AND attributes['unhandled'] = '1'
  AND ts > now() - INTERVAL 15 MINUTE
GROUP BY alert_key
HAVING value >= 5
