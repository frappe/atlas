-- title: Runway Cache Fallbacks
-- resource_type: Atlas
-- section: Runway
-- unit: %
-- style: Line
-- sort_order: 20
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series,
       100 * sumIf(increase, metric = 'runway_cache_fallback_total') / nullIf(sum(increase), 0) AS value
FROM (
    SELECT s.ts AS sample_ts, s.metric AS metric, concat(s.labels['region'], '/', s.labels['pool']) AS series,
           if(s.value < previous, s.value, s.value - previous) AS increase,
           lagInFrame(s.value, 1, s.value) OVER w AS previous
    FROM datum.samples AS s
    WHERE {resource_id:String} = 'atlas' AND s.metric IN ('runway_cache_routed_total', 'runway_cache_fallback_total')
      AND s.ts >= {start:DateTime64(3)} - toIntervalSecond({step:UInt32}) AND s.ts < {end:DateTime64(3)}
    WINDOW w AS (PARTITION BY s.resource_id, s.metric, s.labels ORDER BY s.ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
WHERE sample_ts >= {start:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
