-- title: VM Allocation
-- resource_type: Metal Host
-- section: VMs
-- unit: %
-- style: Line
-- is_key_chart: 0
-- sort_order: 40
-- The CPU, memory and disk of the VMs that metald runs, as a share of the host capacity that Atlas places VMs on.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts,
       multiIf(s.metric IN ('metal_vm_allocated_cpu_millicores', 'atlas_host_cpu_millicores'), 'CPU',
               s.metric IN ('metal_vm_allocated_memory_bytes', 'atlas_host_memory_bytes'), 'Memory', 'Disk') AS series,
       100 * avgIf(s.value, s.resource_id = {resource_id:String}) / nullIf(avgIf(s.value, s.resource_id = 'atlas'), 0) AS value
FROM datum.samples AS s
WHERE ((s.metric IN ('metal_vm_allocated_cpu_millicores', 'metal_vm_allocated_memory_bytes', 'metal_vm_allocated_disk_bytes') AND s.resource_id = {resource_id:String})
        OR (s.metric IN ('atlas_host_cpu_millicores', 'atlas_host_memory_bytes', 'atlas_host_storage_bytes') AND s.resource_id = 'atlas'
            AND s.labels['server'] = {resource_id:String} AND s.labels['kind'] = 'total'))
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
