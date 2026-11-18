-- title: Power
-- resource_type: Metal Host
-- requires_metric: bmc_power_watts
-- section: Hardware
-- unit: W
-- style: Line
-- is_key_chart: 0
-- sort_order: 10
-- The power that the BMC measures now, by sensor, and the output of each power supply. A failed power supply drops to 0.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, if(s.metric = 'bmc_power_watts', s.labels['name'], concat(s.labels['name'], ' output')) AS series,
       avg(s.value) AS value
FROM datum.samples AS s
WHERE ((s.metric = 'bmc_power_watts' AND s.labels['kind'] = 'consumed') OR s.metric = 'bmc_power_supply_output_watts') AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
