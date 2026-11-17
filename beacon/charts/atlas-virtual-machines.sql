-- title: VMs by State
-- resource_type: Atlas
-- section: Inventory
-- unit:
-- style: Line
-- is_key_chart: 0
-- sort_order: 10
-- The VMs of all hosts, and the VMs that Atlas terminates now in any state. Each collection is one sample of each host and state, so the query adds the hosts and then takes the mean in the step.
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, avg(total) AS value
FROM (
    SELECT s.ts AS sample_ts, if(s.metric = 'atlas_virtual_machines_terminating', 'Terminating', initcap(s.labels['state'])) AS series, sum(s.value) AS total
    FROM datum.samples AS s
    WHERE s.metric IN ('atlas_virtual_machines', 'atlas_virtual_machines_terminating') AND s.resource_id = {resource_id:String}
        AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
    GROUP BY sample_ts, series
)
GROUP BY ts, series
ORDER BY ts, series
