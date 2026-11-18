-- title: Few public IPv4 addresses left
-- description: The region has too few free public IPv4 addresses. New VMs cannot get one.
-- severity: High
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/atlas-public-ips-exhausted.md
-- charts: atlas-public-ips
-- interval: 300
-- pending_for: 300
-- resolve_after: 600
-- Atlas creates allocation rows on demand, so free addresses are the capacity minus the rows in use.
-- Both metrics come from the same Atlas collection, so the query reads the latest one.
-- It counts all IPv4 pools together, because a pool with one address for one service is full by design.
-- No free address pages, because new VMs cannot get one. Fewer than 5, or under 10% free, is Medium.
SELECT 'atlas' AS resource_id, '' AS alert_key, sum(capacity) - sum(used) AS value,
       concat(toString(value), ' of ', toString(sum(capacity)), ' public IPv4 addresses free (limit 5 or 10%)') AS detail,
       if(value <= 0, 'High', 'Medium') AS severity
FROM (
    SELECT labels['pool'] AS pool,
           maxIf(value, metric = 'atlas_public_ip_capacity') AS capacity,
           sumIf(value, metric = 'atlas_public_ip_allocations' AND labels['status'] != 'Available') AS used
    FROM datum.samples_recent
    WHERE metric IN ('atlas_public_ip_capacity', 'atlas_public_ip_allocations') AND labels['version'] = '4' AND resource_id = 'atlas'
      AND ts = (SELECT max(ts) FROM datum.samples_recent
                WHERE metric = 'atlas_public_ip_capacity' AND resource_id = 'atlas' AND ts > now() - INTERVAL 15 MINUTE)
    GROUP BY pool
)
HAVING sum(capacity) > 0 AND (value < 5 OR value < 0.1 * sum(capacity))
