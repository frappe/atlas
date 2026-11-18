-- title: Runner Pool Ages
-- resource_type: Atlas
-- section: Runway
-- unit: s
-- style: Line
-- sort_order: 10
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts,
       concat(s.labels['region'], '/', s.labels['pool'], ' ',
              multiIf(s.metric = 'runway_controller_ownership_renewal_age_seconds', 'renewal',
                      s.metric = 'runway_controller_no_progress_seconds', 'waiting demand', 'lease cleanup')) AS series,
       max(s.value) AS value
FROM datum.samples AS s
WHERE {resource_id:String} = 'atlas'
  AND s.metric IN ('runway_controller_ownership_renewal_age_seconds', 'runway_controller_no_progress_seconds',
                   'runway_controller_cleanup_oldest_seconds')
  AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
