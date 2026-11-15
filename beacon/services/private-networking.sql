-- title: Private networking
-- description: Servers reach each other over the private network.
-- scope: Region
-- status_page_group: Networking
-- slo_target: 99.9
-- sort_order: 6
-- Each host sends metal_network_mesh_peer_connected for each peer host: 1 when their WireGuard tunnel had a handshake in the last 3 minutes.
-- Only the hosts that Atlas lists count, and only tunnels to peers that still send, so a host that is down does not count against the others.
SELECT resource_id,
       toInt64(sum(connected)) AS good,
       toInt64(count()) AS valid,
       max(last_ts) AS last_ts
FROM (
    SELECT resource_id, labels['peer'] AS peer, argMax(value, ts) AS connected, max(ts) AS last_ts
    FROM datum.samples_recent
    WHERE metric = 'metal_network_mesh_peer_connected' AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY resource_id, peer
)
WHERE resource_id IN (
    SELECT labels['machine'] FROM datum.samples_recent
    WHERE metric = 'atlas_machine' AND resource_id = 'atlas' AND labels['kind'] = 'host' AND ts > now() - INTERVAL 15 MINUTE
)
  AND peer IN (
    SELECT DISTINCT resource_id FROM datum.samples_recent
    WHERE metric = 'metal_network_mesh_peer_connected' AND ts > now() - INTERVAL 3 MINUTE
)
GROUP BY resource_id
