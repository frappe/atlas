-- title: Host Sync Age
-- resource_type: Atlas
-- section: Metal
-- unit: s
-- style: Line
-- is_key_chart: 0
-- sort_order: 20
-- The time since Atlas synced the usage of each host, and since its last full Warpgate sync of all hosts.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, if(s.metric = 'atlas_warpgate_sync_age_seconds', 'Warpgate', s.labels['server']) AS series,
       max(s.value) AS value
FROM datum.samples AS s
WHERE s.metric IN ('atlas_host_sync_age_seconds', 'atlas_warpgate_sync_age_seconds') AND s.resource_id = {resource_id:String} AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
