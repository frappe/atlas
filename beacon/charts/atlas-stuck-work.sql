-- title: Oldest Open Work
-- resource_type: Atlas
-- section: Jobs
-- unit: min
-- style: Line
-- is_key_chart: 0
-- sort_order: 30
-- The age of the oldest VM draft and the oldest active migration, as the atlas-stuck-work rule reads them.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts,
       if(s.metric = 'atlas_virtual_machine_oldest_draft_seconds', 'VM draft', 'Migration') AS series, max(s.value) / 60 AS value
FROM datum.samples AS s
WHERE s.metric IN ('atlas_virtual_machine_oldest_draft_seconds', 'atlas_migration_oldest_active_seconds') AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
