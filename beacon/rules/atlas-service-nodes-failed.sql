-- title: Atlas service nodes have failed
-- description: Nodes of an Atlas service have failed. Traffic can be down without a readiness alert.
-- severity: High
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/atlas-service-nodes-failed.md
-- charts: atlas-service-nodes
-- interval: 60
-- pending_for: 120
-- resolve_after: 300
-- A Failed node has no readiness sample, so Service node not ready cannot see it. Only the latest Atlas collection counts,
-- because Atlas sends no sample for a status that no node has.
-- affected names each failed node and its VM, from atlas_service_node_failed.
SELECT 'atlas' AS resource_id, f.service AS alert_key, failed AS value,
       concat(toString(failed), ' ', f.service, ' nodes have failed') AS detail,
       n.affected AS affected
FROM (
    SELECT labels['service'] AS service, sum(value) AS failed
    FROM datum.samples_recent
    WHERE metric = 'atlas_service_servers' AND resource_id = 'atlas' AND labels['status'] = 'Failed'
      AND ts = (SELECT max(ts) FROM datum.samples_recent
                WHERE metric = 'atlas_service_servers' AND resource_id = 'atlas' AND ts > now() - INTERVAL 10 MINUTE)
    GROUP BY service
) AS f
LEFT JOIN (
    SELECT labels['service'] AS service,
           mapFromArrays(groupArray(50)(labels['node']), groupArray(50)(concat('Failed, on ', if(labels['virtual_machine'] = '', 'no VM', labels['virtual_machine'])))) AS affected
    FROM datum.latest_value(metric = 'atlas_service_node_failed', window = 300)
    WHERE resource_id = 'atlas'
    GROUP BY service
) AS n ON n.service = f.service
WHERE failed > 0
