-- title: Host network errors
-- severity: Medium
-- resource_type: Host
-- interval: 300
-- pending_for: 300
-- resolve_after: 3600
-- A link is down when the interface is up and has no carrier: Medium. More than 10 NIC errors in 1 h are Low.
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
SELECT resource_id, concat('nic-errors:', interface), errors, concat(toString(errors), ' errors on ', interface, ' in 1 h'), 'Low'
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
    GROUP BY resource_id, interface
)
WHERE errors > 10
