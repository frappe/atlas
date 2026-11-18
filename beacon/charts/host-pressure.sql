-- title: Pressure Stall
-- resource_type: Metal Host
-- section: CPU
-- unit: %
-- style: Line
-- is_key_chart: 0
-- sort_order: 30
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, sum(increase) * 100 / nullIf(sum(seconds), 0) AS value
FROM (
    SELECT s.ts AS sample_ts, concat(s.labels['resource'], ' ', s.labels['kind']) AS series,
           s.value - lagInFrame(s.value, 1, s.value) OVER w AS increase,
           dateDiff('millisecond', lagInFrame(s.ts, 1, s.ts) OVER w, s.ts) / 1000 AS seconds
    FROM datum.samples AS s
    WHERE s.metric = 'system_pressure_stall_seconds_total' AND s.resource_id = {resource_id:String}
        AND s.ts >= {start:DateTime64(3)} - toIntervalSecond({step:UInt32}) AND s.ts < {end:DateTime64(3)}
    WINDOW w AS (PARTITION BY s.metric, s.labels ORDER BY s.ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
WHERE sample_ts >= {start:DateTime64(3)} AND increase >= 0
GROUP BY ts, series
ORDER BY ts, series
