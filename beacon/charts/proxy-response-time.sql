-- title: Response Time
-- resource_type: Proxy Server
-- section: Requests
-- unit: s
-- style: Line
-- is_key_chart: 1
-- sort_order: 20
-- The mean time of a request, from the time counters and the request count. Upstream is the time that the site used.
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, pair.1 AS series, sum(pair.2) / nullIf(sum(requests), 0) AS value
FROM (
    SELECT sample_ts, sumIf(increase, metric = 'proxy_requests_total') AS requests,
           sumIf(increase, metric = 'proxy_request_seconds_total') AS request_seconds,
           sumIf(increase, metric = 'proxy_upstream_seconds_total') AS upstream_seconds
    FROM (
        SELECT s.ts AS sample_ts, s.metric AS metric, s.value - lagInFrame(s.value, 1, s.value) OVER w AS increase
        FROM datum.samples AS s
        WHERE s.metric IN ('proxy_requests_total', 'proxy_request_seconds_total', 'proxy_upstream_seconds_total')
            AND s.resource_id IN (
                SELECT labels['virtual_machine'] FROM datum.samples
                WHERE metric = 'atlas_service_node' AND resource_id = 'atlas' AND labels['node'] = {resource_id:String}
                    AND ts >= {start:DateTime64(3)} - INTERVAL 15 MINUTE AND ts < {end:DateTime64(3)}
            )
            AND s.ts >= {start:DateTime64(3)} - toIntervalSecond({step:UInt32}) AND s.ts < {end:DateTime64(3)}
        WINDOW w AS (PARTITION BY s.resource_id, s.metric, s.labels ORDER BY s.ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
    )
    WHERE sample_ts >= {start:DateTime64(3)} AND increase >= 0
    GROUP BY sample_ts
)
ARRAY JOIN [('Request', request_seconds), ('Upstream', upstream_seconds)] AS pair
GROUP BY ts, series
ORDER BY ts, series
