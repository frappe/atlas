-- title: Disk Space Used
-- resource_type: Metal Host, Virtual Machine
-- section: Disk
-- unit: %
-- style: Line
-- is_key_chart: 1
-- sort_order: 10
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, s.labels['mountpoint'] AS series,
       (1 - avgIf(s.value, s.metric = 'system_filesystem_available_bytes')
           / nullIf(avgIf(s.value, s.metric = 'system_filesystem_size_bytes'), 0)) * 100 AS value
FROM datum.samples AS s
WHERE s.metric IN ('system_filesystem_size_bytes', 'system_filesystem_available_bytes') AND s.labels['type'] != 'tmpfs'
    AND s.resource_id = {resource_id:String} AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
