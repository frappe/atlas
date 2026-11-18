-- title: Program Attached
-- resource_type: IPv6 Router Server
-- section: Router
-- unit: 
-- style: Line
-- is_key_chart: 1
-- sort_order: 10
-- 1 when the router program is attached, as the ipv6-router-detached rule reads it. The VM of the node sends it.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, 'Attached' AS series, min(s.value) AS value
FROM datum.samples AS s
WHERE s.metric = 'ipv6_router_program_attached'
    AND s.resource_id IN (
        SELECT labels['virtual_machine'] FROM datum.samples
        WHERE metric = 'atlas_service_node' AND resource_id = 'atlas' AND labels['node'] = {resource_id:String}
            AND ts >= {start:DateTime64(3)} - INTERVAL 15 MINUTE AND ts < {end:DateTime64(3)}
    )
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
