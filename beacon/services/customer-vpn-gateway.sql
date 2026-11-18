-- title: Customer VPN gateway
-- description: Customers reach their private network through the VPN gateway.
-- scope: Region
-- status_page_group: Networking
-- slo_target: 99.9
-- sort_order: 7
-- Atlas probes /healthz of each active gateway node each minute and sends atlas_service_healthy. It passes while the node has its peer state and wg0 is configured.
SELECT node AS resource_id,
       toInt64(argMax(value, ts) = 1) AS good,
       toInt64(1) AS valid,
       max(ts) AS last_ts
FROM (
    SELECT labels['node'] AS node, value, ts
    FROM datum.samples_recent
    WHERE metric = 'atlas_service_healthy' AND resource_id = 'atlas' AND labels['service'] = 'wireguard_gateway' AND ts > now() - INTERVAL 10 MINUTE
)
GROUP BY node
