-- title: Swap Used
-- resource_type: Virtual Machine
-- requires_metric: system_memory_swap_used_bytes
-- section: Memory
-- unit: bytes
-- style: Line
-- is_key_chart: 0
-- sort_order: 20
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, 'Used' AS series, max(s.value) AS value
FROM datum.samples AS s
WHERE s.metric = 'system_memory_swap_used_bytes' AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
