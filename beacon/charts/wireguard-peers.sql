-- title: Peers
-- resource_type: Wireguard Gateway Server
-- section: Peers
-- unit: 
-- style: Line
-- is_key_chart: 1
-- sort_order: 10
-- The VM of the node sends the counts. An active peer had a handshake in the last 3 minutes.
SELECT toStartOfInterval(s.ts, toIntervalSecond({step:UInt32})) AS ts, if(s.metric = 'wireguard_peers', 'All', 'Active') AS series, max(s.value) AS value
FROM datum.samples AS s
WHERE s.metric IN ('wireguard_peers', 'wireguard_peers_active')
    AND s.resource_id IN (
        SELECT labels['virtual_machine'] FROM datum.samples
        WHERE metric = 'atlas_service_node' AND resource_id = 'atlas' AND labels['node'] = {resource_id:String}
            AND ts >= {start:DateTime64(3)} - INTERVAL 15 MINUTE AND ts < {end:DateTime64(3)}
    )
    AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
GROUP BY ts, series
ORDER BY ts, series
