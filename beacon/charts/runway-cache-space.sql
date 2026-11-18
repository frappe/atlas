-- title: Runway Cache Free Space
-- resource_type: Virtual Machine
-- requires_metric: runway_cache_filesystem_size_bytes
-- section: Disk
-- unit: %
-- style: Line
-- sort_order: 20
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, s.labels['area'] AS series,
       100 * argMaxIf(s.value, s.ts, s.metric = 'runway_cache_filesystem_available_bytes')
           / nullIf(argMaxIf(s.value, s.ts, s.metric = 'runway_cache_filesystem_size_bytes'), 0) AS value
FROM datum.samples AS s
WHERE s.resource_id = {resource_id:String}
  AND s.metric IN ('runway_cache_filesystem_available_bytes', 'runway_cache_filesystem_size_bytes')
  AND s.labels['area'] IN ('journal', 'spool')
  AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
HAVING countIf(s.metric = 'runway_cache_filesystem_available_bytes') > 0
ORDER BY ts, series
