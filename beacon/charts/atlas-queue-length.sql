-- title: Queue Length
-- resource_type: Atlas
-- section: Jobs
-- unit:
-- style: Line
-- is_key_chart: 1
-- sort_order: 10
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, s.labels['queue'] AS series, max(s.value) AS value
FROM datum.samples AS s
WHERE s.metric = 'atlas_job_queue_length' AND s.resource_id = {resource_id:String} AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
