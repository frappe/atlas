-- title: Service node not ready
-- severity: High
-- resource_type: Proxy Server, Wireguard Gateway Server, IPv6 Router Server
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/service-node-not-ready.md
-- interval: 60
-- pending_for: 60
-- resolve_after: 120
-- Atlas sends the readiness of each node, so only its rows count: a node label from another sender would name any node.
-- Atlas probes each node once a minute. The last 2 probes must fail, and a third run must confirm, so the alert fires about 3 minutes after the node stops.
SELECT node AS resource_id, service AS alert_key, ready AS value, concat(service, ' on ', node, ' is not ready') AS detail
FROM (
    SELECT labels['node'] AS node, labels['service'] AS service, max(value) AS ready, count() AS samples
    FROM datum.samples_recent
    WHERE metric = 'atlas_service_ready' AND resource_id = 'atlas' AND ts > now() - INTERVAL 150 SECOND
    GROUP BY node, service
)
WHERE ready = 0 AND samples >= 2
