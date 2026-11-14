-- title: Atlas cannot reach metald
-- severity: High
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/atlas-metal-errors.md
-- interval: 60
-- pending_for: 180
-- resolve_after: 300
SELECT 'atlas' AS resource_id, route AS alert_key, errors AS value,
       concat(toString(errors), ' of ', toString(requests), ' calls to metald ', route, ' failed in 10 min') AS detail
FROM (
    SELECT labels['route'] AS route,
           -- 404 and 409 are normal answers. Count only failures to reach metald or its own errors.
           sumIf(increase, labels['status'] = 'unreachable' OR startsWith(labels['status'], '5')) AS errors,
           sum(increase) AS requests
    FROM datum.counter_increase(metric = 'atlas_metal_requests_total', window = 600)
    GROUP BY route
)
WHERE errors >= 5 AND errors > 0.2 * requests
