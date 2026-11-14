-- title: Proxy errors
-- severity: Medium
-- resource_type: Proxy Server
-- interval: 60
-- pending_for: 120
-- resolve_after: 300
-- A proxy error is a 5xx that the proxy made itself. A 5xx that the site sent (origin upstream) is the site's own fault, so it counts only as a request.
-- The rule fires on a spike, 5 times the error share of the last 70 minutes, or on more than 5% errors for 70 minutes, so a steady failure does not hide in its own baseline.
SELECT resource_id, '' AS alert_key, recent_share AS value,
       concat('proxy 5xx share ', toString(round(100 * recent_share, 2)), '% in the last 10 min, ',
              toString(round(100 * baseline_share, 2)), '% in the last 70 min') AS detail
FROM (
    SELECT resource_id,
           sumIf(increase, is_recent AND is_proxy_error) / greatest(sumIf(increase, is_recent), 1) AS recent_share,
           sumIf(increase, NOT is_recent AND is_proxy_error) / greatest(sumIf(increase, NOT is_recent), 1) AS baseline_share,
           sumIf(increase, is_recent) AS recent_requests,
           sumIf(increase, NOT is_recent) AS baseline_requests
    FROM (
        -- counter_increase handles counter resets. The baseline is the last 70 minutes, the recent window included.
        SELECT resource_id, 1 AS is_recent, labels['status_class'] = '5xx' AND labels['origin'] != 'upstream' AS is_proxy_error, increase
        FROM datum.counter_increase(metric = 'service_proxy_requests_total', window = 600)
        UNION ALL
        SELECT resource_id, 0, labels['status_class'] = '5xx' AND labels['origin'] != 'upstream', increase
        FROM datum.counter_increase(metric = 'service_proxy_requests_total', window = 4200)
    )
    GROUP BY resource_id
)
WHERE (recent_requests >= 100 AND recent_share > 0.01 AND recent_share > 5 * baseline_share)
   OR (baseline_requests >= 100 AND baseline_share > 0.05)
