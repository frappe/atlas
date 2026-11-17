-- title: Fans
-- resource_type: Metal Host
-- requires_metric: bmc_fan_speed
-- section: Hardware
-- unit: RPM
-- style: Line
-- is_key_chart: 0
-- sort_order: 30
-- The slowest and the fastest fan that the BMC reads. A failed fan pulls the slowest fan to 0.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, series, if(series = 'Slowest fan', min(s.value), max(s.value)) AS value
FROM datum.samples AS s
ARRAY JOIN ['Slowest fan', 'Fastest fan'] AS series
WHERE s.metric = 'bmc_fan_speed' AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
