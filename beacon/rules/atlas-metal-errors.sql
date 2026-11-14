-- title: Atlas cannot reach metald
-- severity: High
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/atlas-metal-errors.md
-- interval: 60
-- pending_for: 180
-- resolve_after: 300
-- 404 and 409 are normal answers. A burst of failures to reach metald, or of its own errors, pages. Failures that repeat for 1 h,
-- and any 401 or 403, which means Atlas and metald disagree on certificates or tokens, are Medium.
SELECT 'atlas' AS resource_id, alert_key, value, detail, severity
FROM (
    SELECT route AS alert_key, recent_errors AS value,
           concat(toString(recent_errors), ' of ', toString(recent_requests), ' calls to metald ', route, ' failed in 10 min') AS detail,
           'High' AS severity
    FROM (
        SELECT labels['route'] AS route,
               sumIf(increase, labels['status'] = 'unreachable' OR startsWith(labels['status'], '5')) AS recent_errors,
               sum(increase) AS recent_requests
        FROM datum.counter_increase(metric = 'atlas_metal_requests_total', window = 600)
        GROUP BY route
    )
    WHERE recent_errors >= 5 AND recent_errors > 0.2 * recent_requests
    UNION ALL
    SELECT concat('hourly:', route), errors, concat(toString(errors), ' calls to metald ', route, ' failed in 1 h'), 'Medium'
    FROM (
        SELECT labels['route'] AS route,
               sumIf(increase, labels['status'] = 'unreachable' OR startsWith(labels['status'], '5')) AS errors
        FROM datum.counter_increase(metric = 'atlas_metal_requests_total', window = 3600)
        GROUP BY route
    )
    WHERE errors >= 5
    UNION ALL
    SELECT 'auth', denied, concat(toString(denied), ' calls to metald were refused with 401 or 403 in 1 h'), 'Medium'
    FROM (
        SELECT sumIf(increase, labels['status'] IN ('401', '403')) AS denied
        FROM datum.counter_increase(metric = 'atlas_metal_requests_total', window = 3600)
    )
    WHERE denied > 0
)
