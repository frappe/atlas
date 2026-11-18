-- title: VMs Not Healthy
-- resource_type: Metal Host
-- section: VMs
-- unit: 
-- style: Line
-- is_key_chart: 0
-- sort_order: 20
-- The VMs that metald cannot keep as wanted, as the metald-unhealthy and vms-unavailable-on-host rules read them.
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, max(total) AS value
FROM (
    SELECT s.ts AS sample_ts,
           multiIf(s.metric = 'metal_vm_failing', 'Failing', s.metric = 'metal_vm_pending', 'Pending', concat('Unavailable, ', replaceAll(s.labels['reason'], '_', ' '))) AS series,
           sum(s.value) AS total
    FROM datum.samples AS s
    WHERE s.metric IN ('metal_vm_failing', 'metal_vm_pending', 'metal_vm_unavailable') AND s.resource_id = {resource_id:String}
        AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
    GROUP BY sample_ts, series
)
GROUP BY ts, series
ORDER BY ts, series
