-- title: Error Logs per Minute
-- resource_type: Atlas
-- section: API
-- unit:
-- style: Bar
-- is_key_chart: 1
-- sort_order: 20
-- Atlas counts the new Error Logs of the last minute each minute. A wide step shows its busiest minute.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, 'Error Logs' AS series, max(s.value) AS value
FROM datum.samples AS s
WHERE s.metric = 'atlas_error_logs_last_minute' AND s.resource_id = {resource_id:String} AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
