-- title: Mesh Peers
-- resource_type: Metal Host
-- section: Network
-- unit:
-- style: Line
-- is_key_chart: 0
-- sort_order: 30
-- A connected peer host had a WireGuard handshake on wg0 in the last 3 minutes. The default route link is 1 while the device of the IPv4 default route is up.
SELECT toStartOfInterval(sample_ts, toIntervalSecond({step:UInt32})) AS ts, pair.1 AS series, if(series = 'Not connected', max(pair.2), min(pair.2)) AS value
FROM (
    SELECT s.ts AS sample_ts, s.metric AS metric, sum(s.value) AS up, count() AS total
    FROM datum.samples AS s
    WHERE s.metric IN ('metal_network_mesh_peer_connected', 'metal_network_public_up') AND s.resource_id = {resource_id:String}
        AND s.ts >= {start:DateTime64(3)} AND s.ts < {end:DateTime64(3)}
    GROUP BY sample_ts, metric
)
ARRAY JOIN if(metric = 'metal_network_public_up', [('Default route link up', up)], [('Connected', up), ('Not connected', total - up)]) AS pair
GROUP BY ts, series
ORDER BY ts, series
