-- title: metald Process
-- resource_type: Metal Host
-- section: Metal
-- unit:
-- style: Line
-- is_key_chart: 0
-- sort_order: 30
-- A leak in metald shows as memory or goroutines that grow and do not come down.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts,
       if(s.metric = 'metal_process_resident_memory_bytes', 'Resident memory (MiB)', 'Goroutines') AS series,
       max(if(s.metric = 'metal_process_resident_memory_bytes', s.value / 1048576, s.value)) AS value
FROM datum.samples AS s
WHERE s.metric IN ('metal_process_resident_memory_bytes', 'metal_process_goroutines') AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
