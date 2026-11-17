-- title: NVMe Wear
-- resource_type: Metal Host
-- requires_metric: system_nvme_smart
-- section: Disk
-- unit: %
-- style: Line
-- is_key_chart: 0
-- sort_order: 50
-- Used is the share of the rated endurance that the drive used. Spare is the reserve of blocks left to replace bad blocks.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts,
       concat(s.labels['device'], if(s.labels['field'] = 'percentage_used', ' used', ' spare')) AS series, max(s.value) AS value
FROM datum.samples AS s
WHERE s.metric = 'system_nvme_smart' AND s.labels['field'] IN ('percentage_used', 'available_spare_percent') AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
