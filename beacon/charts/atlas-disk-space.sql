-- title: Disk Space Used
-- resource_type: Atlas
-- section: Disk
-- unit: %
-- style: Line
-- is_key_chart: 1
-- sort_order: 10
-- The root, bench and MariaDB file systems of the Atlas site.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, s.labels['path'] AS series,
       (1 - avgIf(s.value, s.labels['kind'] = 'available') / nullIf(avgIf(s.value, s.labels['kind'] = 'size'), 0)) * 100 AS value
FROM datum.samples AS s
WHERE s.metric = 'atlas_filesystem_bytes' AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
