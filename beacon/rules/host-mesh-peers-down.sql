-- title: Host mesh peers not connected
-- description: The host has no WireGuard tunnel to some peer hosts. Private traffic of VMs between these hosts fails.
-- severity: High
-- resource_type: Metal Host
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/host-mesh-peers-down.md
-- charts: host-mesh-peers
-- interval: 60
-- pending_for: 300
-- resolve_after: 300
-- metald sends 0 for a peer without a handshake on wg0 in the last 3 minutes. Only Running hosts count, as a host and as a peer.
-- A peer that does not report is left out: host-down covers it. A host fires only when it lost at least half of its peers,
-- so a host with a broken wg0 fires once, and the hosts that lost only that one peer do not.
WITH running AS (
    SELECT labels['server'] AS server, argMax(labels['title'], ts) AS title
    FROM datum.samples_recent
    WHERE metric = 'atlas_metal_server_info' AND resource_id = 'atlas' AND ts > now() - INTERVAL 10 MINUTE
    GROUP BY server
    HAVING argMax(labels['status'], ts) = 'Running'
)
SELECT c.resource_id AS resource_id, '' AS alert_key, toFloat64(countIf(c.is_down)) AS value,
       concat(toString(countIf(c.is_down)), ' of ', toString(count()), ' mesh peers have no WireGuard handshake: ',
              arrayStringConcat(arraySlice(arraySort(groupArrayIf(p.title, c.is_down)), 1, 3), ', ')) AS detail
FROM (
    SELECT resource_id, labels['peer'] AS peer, value = 0 AS is_down
    FROM datum.latest_value(metric = 'metal_network_mesh_peer_connected', window = 300)
) AS c
INNER JOIN running AS p ON p.server = c.peer
WHERE c.resource_id IN (SELECT server FROM running)
    AND c.peer IN (SELECT resource_id FROM datum.samples_recent WHERE metric = 'metal_build_info' AND ts > now() - INTERVAL 5 MINUTE)
GROUP BY c.resource_id
HAVING countIf(c.is_down) > 0 AND countIf(c.is_down) * 2 >= count()
