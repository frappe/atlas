-- title: Proxy error spike
-- severity: Medium
-- resource_type: Proxy Server
-- interval: 60
-- pending_for: 120
-- resolve_after: 300
SELECT resource_id, '' AS alert_key, recent_share AS value,
       concat('5xx share ', toString(round(100 * recent_share, 2)), '% in the last 10 min, ',
              toString(round(100 * baseline_share, 2)), '% in the last 70 min') AS detail
FROM (
    SELECT resource_id,
           sumIf(increase, is_recent AND status_class = '5xx') / greatest(sumIf(increase, is_recent), 1) AS recent_share,
           sumIf(increase, NOT is_recent AND status_class = '5xx') / greatest(sumIf(increase, NOT is_recent), 1) AS baseline_share,
           sumIf(increase, is_recent) AS recent_requests
    FROM (
        -- counter_increase handles counter resets. The baseline is the last 70 minutes, the recent window included.
        SELECT resource_id, 1 AS is_recent, labels['status_class'] AS status_class, increase
        FROM datum.counter_increase(metric = 'service_proxy_requests_total', window = 600)
        UNION ALL
        SELECT resource_id, 0, labels['status_class'], increase
        FROM datum.counter_increase(metric = 'service_proxy_requests_total', window = 4200)
    )
    GROUP BY resource_id
)
-- A spike: at least 100 requests, more than 1% errors, and 5 times the error share of the last 70 minutes.
WHERE recent_requests >= 100 AND recent_share > 0.01 AND recent_share > 5 * baseline_share
