-- title: Atlas unhandled errors
-- severity: Medium
-- resource_type: Atlas
-- interval: 60
-- pending_for: 60
-- resolve_after: 900
-- One alert for each error fingerprint that is new in the last hour (not seen in the day before), that repeats 5 times in 15 min, or 20 times in 1 h.
-- Errors that Atlas logs on purpose are handled and not counted. A PermissionError is a caller without access, such as a guest, not an Atlas failure.
SELECT 'atlas' AS resource_id, alert_key, toFloat64(hour_count) AS value,
       concat(if(is_new, 'new: ', ''), toString(hour_count), ' x ', first_message, ' in 1 h') AS detail
FROM (
    SELECT if(attributes['fingerprint'] = '', message, attributes['fingerprint']) AS alert_key, any(message) AS first_message,
           countIf(ts > now() - INTERVAL 15 MINUTE) AS recent_count, countIf(ts > now() - INTERVAL 1 HOUR) AS hour_count,
           min(ts) > now() - INTERVAL 1 HOUR AS is_new
    FROM datum.logs
    WHERE product = 'atlas' AND source = 'error_log' AND attributes['unhandled'] = '1'
      AND attributes['exception'] != 'frappe.exceptions.PermissionError'
      AND ts > now() - INTERVAL 1 DAY
    GROUP BY alert_key
)
WHERE hour_count > 0 AND (is_new OR recent_count >= 5 OR hour_count >= 20)
