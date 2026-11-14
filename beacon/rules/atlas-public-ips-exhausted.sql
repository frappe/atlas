-- title: Public IPv4 pool almost empty
-- severity: High
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/atlas-public-ips-exhausted.md
-- interval: 300
-- pending_for: 300
-- resolve_after: 600
-- Atlas creates allocation rows on demand, so free addresses are the capacity minus the rows in use.
-- Both metrics come from the same Atlas collection, so the query reads the latest one.
SELECT 'atlas' AS resource_id, pool AS alert_key, capacity - used AS value,
       concat(toString(value), ' of ', toString(capacity), ' IPv4 addresses free in pool ', pool) AS detail
FROM (
    SELECT labels['pool'] AS pool,
           maxIf(value, metric = 'atlas_public_ip_capacity') AS capacity,
           sumIf(value, metric = 'atlas_public_ip_allocations' AND labels['status'] != 'Available') AS used
    FROM datum.samples_recent
    WHERE metric IN ('atlas_public_ip_capacity', 'atlas_public_ip_allocations') AND labels['version'] = '4'
      AND ts = (SELECT max(ts) FROM datum.samples_recent
                WHERE metric = 'atlas_public_ip_capacity' AND ts > now() - INTERVAL 15 MINUTE)
    GROUP BY pool
)
WHERE capacity >= 16 AND value < 0.1 * capacity
