-- title: Reconcile Loop Age
-- resource_type: Metal Host
-- section: Metal
-- unit: s
-- style: Line
-- is_key_chart: 0
-- sort_order: 20
-- The time since each metald loop finished a pass, as the metald-unhealthy rule reads it. The image loop runs each hour.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, s.labels['loop'] AS series, max(toUnixTimestamp(s.ts) - s.value) AS value
FROM datum.samples AS s
WHERE s.metric = 'metal_reconciler_last_pass_timestamp_seconds' AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
