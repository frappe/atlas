-- title: Jobs
-- resource_type: Atlas
-- section: Jobs
-- unit:
-- style: Line
-- is_key_chart: 0
-- sort_order: 20
-- The job registries of all queues. Failed keeps a failed job until it expires.
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, avg(total) AS value
FROM (
    SELECT s.ts AS sample_ts, if(s.metric = 'atlas_job_started', 'Running', 'Failed') AS series, sum(s.value) AS total
    FROM datum.samples AS s
    WHERE s.metric IN ('atlas_job_started', 'atlas_job_failed') AND s.resource_id = {resource_id:String}
        AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
    GROUP BY sample_ts, series
)
GROUP BY ts, series
ORDER BY ts, series
