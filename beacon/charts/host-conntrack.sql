-- title: Conntrack Table Used
-- resource_type: Metal Host
-- section: Kernel
-- unit: %
-- style: Line
-- is_key_chart: 0
-- sort_order: 10
-- A full conntrack table drops new connections. The host-limits rule reads the same share.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, 'Used' AS series,
       100 * avgIf(s.value, s.metric = 'system_network_conntrack_entries') / nullIf(avgIf(s.value, s.metric = 'system_network_conntrack_entries_limit'), 0) AS value
FROM datum.samples AS s
WHERE s.metric IN ('system_network_conntrack_entries', 'system_network_conntrack_entries_limit') AND s.resource_id = {resource_id:String}
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
