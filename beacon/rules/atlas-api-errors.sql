-- title: Atlas API errors
-- severity: High
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/atlas-api-errors.md
-- interval: 60
-- pending_for: 120
-- resolve_after: 300
-- One operation with 5 errors that are 5% of its calls in 10 min, or all calls with more than 1% errors in 15 min (at least 50 calls).
SELECT 'atlas' AS resource_id, operation AS alert_key, errors AS value,
       concat(toString(errors), ' of ', toString(requests), ' ', operation, ' calls failed with 5xx in 10 min') AS detail
FROM (
    SELECT labels['operation'] AS operation,
           sumIf(increase, startsWith(labels['status'], '5')) AS errors,
           sum(increase) AS requests
    FROM datum.counter_increase(metric = 'atlas_api_requests_total', window = 600)
    GROUP BY operation
)
WHERE errors >= 5 AND errors > 0.05 * requests
UNION ALL
SELECT 'atlas', 'all', errors, concat(toString(errors), ' of ', toString(requests), ' API calls failed with 5xx in 15 min')
FROM (
    SELECT sumIf(increase, startsWith(labels['status'], '5')) AS errors, sum(increase) AS requests
    FROM datum.counter_increase(metric = 'atlas_api_requests_total', window = 900)
)
WHERE requests >= 50 AND errors > 0.01 * requests
