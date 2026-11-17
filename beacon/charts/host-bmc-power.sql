-- title: Power
-- resource_type: Metal Host
-- requires_metric: bmc_power_watts
-- section: Hardware
-- unit: W
-- style: Line
-- is_key_chart: 0
-- sort_order: 10
-- The power that the BMC measures now, by sensor.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, s.labels['name'] AS series, avg(s.value) AS value
FROM datum.samples AS s
WHERE s.metric = 'bmc_power_watts' AND s.labels['kind'] = 'consumed' AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
