-- title: Hardware Temperature
-- resource_type: Metal Host
-- requires_metric: bmc_temperature_celsius
-- section: Hardware
-- unit: °C
-- style: Line
-- is_key_chart: 0
-- sort_order: 20
-- The hottest BMC sensor, and the smallest gap between a sensor and its critical limit. A small gap matters more than a high value.
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, if(series = 'Hottest sensor', max(hottest), min(margin)) AS value
FROM (
    SELECT s.ts AS sample_ts, max(temperature) AS hottest, minIf(critical - temperature, critical > 0) AS margin
    FROM (
        SELECT s.ts, s.labels['name'] AS name,
               anyIf(s.value, s.metric = 'bmc_temperature_celsius') AS temperature,
               anyIf(s.value, s.metric = 'bmc_temperature_critical_celsius') AS critical
        FROM datum.samples AS s
        WHERE s.metric IN ('bmc_temperature_celsius', 'bmc_temperature_critical_celsius') AND s.resource_id = {resource_id:String}
            AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
        GROUP BY s.ts, name
    ) AS s
    GROUP BY sample_ts
)
ARRAY JOIN ['Hottest sensor', 'Gap to critical limit'] AS series
GROUP BY ts, series
ORDER BY ts, series
