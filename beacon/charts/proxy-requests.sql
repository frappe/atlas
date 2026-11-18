-- title: Requests
-- resource_type: Proxy Server
-- section: Requests
-- unit: req/s
-- style: Line
-- is_key_chart: 1
-- sort_order: 10
-- The VM of the node sends the counts. A 5xx from origin proxy is an error of the proxy, a 5xx from upstream is an error of the site.
SELECT ts, series, sum(rate) AS value
FROM (
    SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, labels, sum(increase) / nullIf(sum(seconds), 0) AS rate
    FROM (
        SELECT s.ts AS sample_ts, s.labels AS labels,
               multiIf(s.labels['status_class'] != '5xx', s.labels['status_class'], s.labels['origin'] = 'upstream', 'Site 5xx', 'Proxy 5xx') AS series,
               s.value - lagInFrame(s.value, 1, s.value) OVER w AS increase,
               dateDiff('millisecond', lagInFrame(s.ts, 1, s.ts) OVER w, s.ts) / 1000 AS seconds
        FROM datum.samples AS s
        WHERE s.metric = 'proxy_requests_total'
            AND s.resource_id IN (
                SELECT labels['virtual_machine'] FROM datum.samples
                WHERE metric = 'atlas_service_node' AND resource_id = 'atlas' AND labels['node'] = {resource_id:String}
                    AND ts >= {start:DateTime64(3)} - INTERVAL 15 MINUTE AND ts < {end:DateTime64(3)}
            )
            AND s.ts >= {start:DateTime64(3)} - toIntervalSecond({step:UInt32}) AND s.ts < {end:DateTime64(3)}
        WINDOW w AS (PARTITION BY s.resource_id, s.labels ORDER BY s.ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
    )
    WHERE sample_ts >= {start:DateTime64(3)} AND increase >= 0
    GROUP BY ts, series, labels
)
GROUP BY ts, series
ORDER BY ts, series
