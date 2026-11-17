-- title: Slow Requests
-- resource_type: Proxy Server
-- section: Requests
-- unit: %
-- style: Line
-- is_key_chart: 0
-- sort_order: 30
-- The share of requests that took 1 second or more. The VM of the node sends the counts.
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, 'Slow' AS series,
       100 * sumIf(increase, metric = 'proxy_slow_requests_total') / nullIf(sumIf(increase, metric = 'proxy_requests_total'), 0) AS value
FROM (
    SELECT s.ts AS sample_ts, s.metric AS metric,
           s.value - lagInFrame(s.value, 1, s.value) OVER w AS increase
    FROM datum.samples AS s
    WHERE s.metric IN ('proxy_requests_total', 'proxy_slow_requests_total')
        AND s.resource_id IN (
            SELECT labels['virtual_machine'] FROM datum.samples
            WHERE metric = 'atlas_service_node' AND resource_id = 'atlas' AND labels['node'] = {resource_id:String}
                AND ts >= {start:DateTime64(3)} - INTERVAL 15 MINUTE AND ts < {end:DateTime64(3)}
        )
        AND s.ts >= {start:DateTime64(3)} - toIntervalSecond({step:UInt32}) AND s.ts < {end:DateTime64(3)}
    WINDOW w AS (PARTITION BY s.resource_id, s.metric, s.labels ORDER BY s.ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
WHERE sample_ts >= {start:DateTime64(3)} AND increase >= 0
GROUP BY ts, series
ORDER BY ts, series
