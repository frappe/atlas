-- title: Public IPs Used
-- resource_type: Atlas
-- section: Inventory
-- unit: %
-- style: Line
-- is_key_chart: 1
-- sort_order: 20
-- The addresses in use as a share of all pools of each IP version. Both metrics come from the same collection.
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, series, avg(used * 100 / nullIf(capacity, 0)) AS value
FROM (
    SELECT s.ts AS sample_ts, concat('IPv', s.labels['version']) AS series,
           sumIf(s.value, s.metric = 'atlas_public_ip_allocations' AND s.labels['status'] != 'Available') AS used,
           sumIf(s.value, s.metric = 'atlas_public_ip_capacity') AS capacity
    FROM datum.samples AS s
    WHERE s.metric IN ('atlas_public_ip_allocations', 'atlas_public_ip_capacity') AND s.resource_id = {resource_id:String}
        AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
    GROUP BY sample_ts, series
)
GROUP BY ts, series
ORDER BY ts, series
