-- title: Clock Offset
-- resource_type: Metal Host
-- section: Kernel
-- unit: ms
-- style: Line
-- is_key_chart: 0
-- sort_order: 20
-- The offset that the kernel still has to correct, with its sign. Each step shows its largest offset.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, 'Offset' AS series, argMax(s.value, abs(s.value)) * 1000 AS value
FROM datum.samples AS s
WHERE s.metric = 'system_timex_offset_seconds' AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
