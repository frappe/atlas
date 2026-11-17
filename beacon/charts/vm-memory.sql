-- title: Memory Used
-- resource_type: Virtual Machine
-- requires_metric: system_memory_total_bytes
-- section: Memory
-- unit: %
-- style: Line
-- is_key_chart: 1
-- sort_order: 10
-- Used is the memory that is not available, as the machine-stress rule reads it.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, 'Used' AS series,
       (1 - avgIf(s.value, s.metric = 'system_memory_available_bytes') / nullIf(avgIf(s.value, s.metric = 'system_memory_total_bytes'), 0)) * 100 AS value
FROM datum.samples AS s
WHERE s.metric IN ('system_memory_total_bytes', 'system_memory_available_bytes') AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
