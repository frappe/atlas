-- title: Image Create Time
-- resource_type: Atlas
-- section: Jobs
-- unit: s
-- style: Bar
-- is_key_chart: 0
-- sort_order: 40
-- The longest VM image creation of each step, by result. Atlas sends one sample for each image.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, initcap(s.labels['result']) AS series, max(s.value) AS value
FROM datum.samples AS s
WHERE s.metric = 'atlas_image_create_duration_seconds' AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
