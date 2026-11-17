-- title: System Load
-- resource_type: Metal Host, Virtual Machine
-- requires_metric: system_load_average
-- section: CPU
-- unit: 
-- style: Line
-- is_key_chart: 0
-- sort_order: 20
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, s.labels['window'] AS series, avg(s.value) AS value
FROM datum.samples AS s
WHERE s.metric = 'system_load_average' AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
