-- title: Host Capacity Used
-- resource_type: Atlas
-- section: Inventory
-- unit: %
-- style: Line
-- is_key_chart: 0
-- sort_order: 30
-- The share of CPU, memory and storage of all hosts that VMs hold, as Atlas allocates it.
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, avg(used) AS value
FROM (
    SELECT s.ts AS sample_ts,
           multiIf(s.metric = 'atlas_host_cpu_millicores', 'CPU', s.metric = 'atlas_host_memory_bytes', 'Memory', 'Storage') AS series,
           100 * (1 - sumIf(s.value, s.labels['kind'] = 'available') / nullIf(sumIf(s.value, s.labels['kind'] = 'total'), 0)) AS used
    FROM datum.samples AS s
    WHERE s.metric IN ('atlas_host_cpu_millicores', 'atlas_host_memory_bytes', 'atlas_host_storage_bytes') AND s.resource_id = {resource_id:String}
        AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
    GROUP BY sample_ts, series
)
GROUP BY ts, series
ORDER BY ts, series
