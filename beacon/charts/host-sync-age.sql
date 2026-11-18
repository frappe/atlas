-- title: Atlas Sync Age
-- resource_type: Metal Host
-- section: Metal
-- unit: s
-- style: Line
-- is_key_chart: 0
-- sort_order: 10
-- The time since Atlas last synced the usage of this host, as the host-down rule reads it. Atlas sends it for each host.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, 'Sync age' AS series, max(s.value) AS value
FROM datum.samples AS s
WHERE s.metric = 'atlas_host_sync_age_seconds' AND s.resource_id = 'atlas' AND s.labels['server'] = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
