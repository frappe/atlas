-- title: Network link down or NIC faults
-- description: A network link is down or a NIC reports errors. Traffic to the host can fail.
-- severity: Medium
-- resource_type: Metal Host
-- interval: 300
-- pending_for: 300
-- resolve_after: 3600
-- A link is down when the interface is up and has no carrier: Medium. A down device of the IPv4 default route, or no default
-- route, is Medium. More than 10 NIC errors in 1 h are Low. Only Ethernet NICs (en*, eth*) count errors: WireGuard counts
-- unauthenticated packets and packets for a peer without a session as errors, and a bond or VLAN repeats the errors of its NICs.
SELECT resource_id, concat('link-down:', interface) AS alert_key, 1 AS value, concat(interface, ' has no carrier') AS detail, 'Medium' AS severity
FROM (
    SELECT resource_id, labels['interface'] AS interface,
           argMaxIf(value, ts, metric = 'system_network_up') AS up, argMaxIf(value, ts, metric = 'system_network_carrier') AS carrier
    FROM datum.samples_recent
    WHERE metric IN ('system_network_up', 'system_network_carrier') AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY resource_id, interface
    HAVING countIf(metric = 'system_network_carrier') > 0
)
WHERE up = 1 AND carrier = 0
UNION ALL
SELECT resource_id, 'default-route-down', 0, 'the device of the IPv4 default route is down, or there is no default route', 'Medium'
FROM datum.latest_value(metric = 'metal_network_public_up', window = 300)
WHERE value = 0
UNION ALL
SELECT resource_id, concat('nic-errors:', interface), errors, concat(toString(errors), ' errors on ', interface, ' in 1 h (limit 10)'), 'Low'
FROM (
    SELECT resource_id, interface, sum(increase) AS errors
    FROM (
        SELECT resource_id, labels['interface'] AS interface, increase
        FROM datum.counter_increase(metric = 'system_network_receive_errors_total', window = 3600)
        UNION ALL
        SELECT resource_id, labels['interface'], increase
        FROM datum.counter_increase(metric = 'system_network_transmit_errors_total', window = 3600)
        UNION ALL
        SELECT resource_id, labels['interface'], increase
        FROM datum.counter_increase(metric = 'system_network_rx_crc_errors_total', window = 3600)
    )
    WHERE match(interface, '^(en|eth)')
    GROUP BY resource_id, interface
)
WHERE errors > 10
