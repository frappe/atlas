-- title: ZFS Pool
-- resource_type: Metal Host
-- section: Disk
-- unit: %
-- style: Line
-- is_key_chart: 0
-- sort_order: 40
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, concat(s.labels['pool'], if(s.metric = 'system_zfs_pool_capacity_percent', ' used', ' fragmentation')) AS series, avg(s.value) AS value
FROM datum.samples AS s
WHERE s.metric IN ('system_zfs_pool_capacity_percent', 'system_zfs_pool_fragmentation_percent') AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
