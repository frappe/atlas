-- title: Public networking
-- description: Customers reach their sites and servers over the internet.
-- scope: Region
-- status_page_group: Networking
-- slo_target: 99.9
-- sort_order: 5
-- Each host that Atlas lists counts once: metald sends metal_network_public_up, 1 while the device of the IPv4 default route is up.
-- Each IPv6 router node counts once: it is good while its tc eBPF program is attached to eth0, because without it the routed IPv6 addresses of the region stop.
-- These checks run inside the region. A cut at the provider shows only through a published incident.
SELECT resource_id,
       toInt64(argMax(value, ts) = 1) AS good,
       toInt64(1) AS valid,
       max(ts) AS last_ts
FROM datum.samples_recent
WHERE metric = 'metal_network_public_up' AND ts > now() - INTERVAL 5 MINUTE
  AND resource_id IN (
      SELECT labels['machine'] FROM datum.samples_recent
      WHERE metric = 'atlas_machine' AND resource_id = 'atlas' AND labels['kind'] = 'host' AND ts > now() - INTERVAL 15 MINUTE
  )
GROUP BY resource_id
UNION ALL
SELECT n.node AS resource_id,
       toInt64(a.attached = 1) AS good,
       toInt64(1) AS valid,
       a.last_ts AS last_ts
FROM (
    SELECT resource_id, argMax(value, ts) AS attached, max(ts) AS last_ts
    FROM datum.samples_recent
    WHERE metric = 'ipv6_router_program_attached' AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY resource_id
) AS a
INNER JOIN (
    SELECT DISTINCT labels['virtual_machine'] AS virtual_machine, labels['node'] AS node
    FROM datum.samples_recent
    WHERE metric = 'atlas_service_node' AND resource_id = 'atlas' AND labels['service'] = 'ipv6_router' AND ts > now() - INTERVAL 15 MINUTE
) AS n ON n.virtual_machine = a.resource_id
