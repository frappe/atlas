-- title: VMs by State
-- resource_type: Metal Host
-- section: VMs
-- unit: 
-- style: Bar
-- is_key_chart: 0
-- sort_order: 10
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, initcap(s.labels['state']) AS series, avg(s.value) AS value
FROM datum.samples AS s
WHERE s.metric = 'metal_vm_count' AND s.labels['state'] != 'destroyed' AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
