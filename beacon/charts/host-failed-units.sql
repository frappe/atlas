-- title: Failed Units
-- resource_type: Metal Host
-- section: Services
-- unit: 
-- style: Line
-- is_key_chart: 0
-- sort_order: 10
-- The systemd units in the failed state, as the host-systemd-units rule reads them.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, 'Failed units' AS series, max(s.value) AS value
FROM datum.samples AS s
WHERE s.metric = 'system_systemd_failed_units' AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
