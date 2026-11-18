-- title: Metal Requests
-- resource_type: Metal Host
-- section: Metal
-- unit: req/s
-- style: Line
-- is_key_chart: 0
-- sort_order: 15
-- Calls from Atlas to metald on this host. Unreachable is a call that got no answer.
SELECT ts, series, sum(rate) AS value
FROM (
    SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, labels, sum(increase) / nullIf(sum(seconds), 0) AS rate
    FROM (
        SELECT s.ts AS sample_ts, s.labels AS labels, if(s.labels['status'] = 'unreachable', 'Unreachable', concat(substring(s.labels['status'], 1, 1), 'xx')) AS series,
               s.value - lagInFrame(s.value, 1, s.value) OVER w AS increase,
               dateDiff('millisecond', lagInFrame(s.ts, 1, s.ts) OVER w, s.ts) / 1000 AS seconds
        FROM datum.samples AS s
        WHERE s.metric = 'atlas_metal_requests_total' AND s.resource_id = 'atlas' AND s.labels['server'] = {resource_id:String}
            AND s.ts >= {start:DateTime64(3)} - toIntervalSecond({step:UInt32}) AND s.ts < {end:DateTime64(3)}
        WINDOW w AS (PARTITION BY s.labels ORDER BY s.ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
    )
    WHERE sample_ts >= {start:DateTime64(3)} AND increase >= 0
    GROUP BY ts, series, labels
)
GROUP BY ts, series
ORDER BY ts, series
