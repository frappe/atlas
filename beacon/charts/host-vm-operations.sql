-- title: VM Operations
-- resource_type: Metal Host
-- section: VMs
-- unit: operations
-- style: Bar
-- is_key_chart: 0
-- sort_order: 30
-- The VM operations that metald finished, with the failures by phase, and the VMs that did not answer a ping after boot.
-- OK leaves out the inspect, network and storage checks that pass for each VM in each loop. metald adds a failed series at its first
-- failure, so a new failed series counts from 0.
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, sum(increase) AS value
FROM (
    SELECT s.ts AS sample_ts,
           multiIf(s.metric = 'metal_vm_boot_unreachable_total', concat('Unreachable after ', s.labels['kind'], ' boot'),
                   s.labels['result'] = 'ok', 'OK', concat('Failed ', s.labels['phase'])) AS series,
           s.value - lagInFrame(s.value, 1, if(s.labels['result'] = 'failed', 0, s.value)) OVER w AS increase
    FROM datum.samples AS s
    WHERE s.metric IN ('metal_vm_operations_total', 'metal_vm_boot_unreachable_total') AND s.resource_id = {resource_id:String}
        AND NOT (s.labels['result'] = 'ok' AND s.labels['phase'] IN ('inspect', 'network', 'storage'))
        AND s.ts >= {start:DateTime64(3)} - toIntervalSecond({step:UInt32}) AND s.ts < {end:DateTime64(3)}
    WINDOW w AS (PARTITION BY s.metric, s.labels ORDER BY s.ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)
)
WHERE sample_ts >= {start:DateTime64(3)} AND increase >= 0
GROUP BY ts, series
ORDER BY ts, series
