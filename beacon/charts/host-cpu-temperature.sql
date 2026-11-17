-- title: CPU Temperature
-- resource_type: Metal Host
-- section: CPU
-- unit: °C
-- style: Line
-- is_key_chart: 0
-- sort_order: 40
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, 'Hottest sensor' AS series, max(s.value) AS value
FROM datum.samples AS s
WHERE s.metric = 'system_hwmon_temperature_celsius' AND s.labels['chip'] IN ('coretemp', 'k10temp') AND s.labels['attribute'] = 'input' AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
