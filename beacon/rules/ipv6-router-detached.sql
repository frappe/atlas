-- title: IPv6 router not forwarding
-- severity: High
-- resource_type: IPv6 Router Server
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/ipv6-router-detached.md
-- interval: 60
-- pending_for: 120
-- resolve_after: 120
-- The router translates in a tc eBPF program on eth0. Without it the VM runs, but forwards nothing, so the public IPv6 addresses of the region stop.
-- The Datum agent of the router VM sends ipv6_router_program_attached each 30 seconds. atlas_service_node names the router node of the VM.
SELECT n.node AS resource_id, '' AS alert_key, a.attached AS value, 'the eBPF program is not attached to eth0' AS detail
FROM (
    SELECT resource_id, argMax(value, ts) AS attached
    FROM datum.samples_recent
    WHERE metric = 'ipv6_router_program_attached' AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY resource_id
) AS a
INNER JOIN (
    SELECT DISTINCT labels['virtual_machine'] AS virtual_machine, labels['node'] AS node
    FROM datum.samples_recent
    WHERE metric = 'atlas_service_node' AND labels['service'] = 'ipv6_router' AND ts > now() - INTERVAL 15 MINUTE
) AS n ON n.virtual_machine = a.resource_id
WHERE a.attached = 0
