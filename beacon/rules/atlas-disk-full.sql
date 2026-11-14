-- title: Atlas disk almost full
-- severity: High
-- resource_type: Atlas
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/atlas-disk-full.md
-- interval: 300
-- pending_for: 300
-- resolve_after: 600
-- Under 10% free pages. Under 25% free is Low: plan space before it fills.
SELECT 'atlas' AS resource_id, path AS alert_key, 100 * available / size AS value,
       concat(path, ' has ', toString(round(value, 1)), '% free') AS detail,
       if(available < 0.1 * size, 'High', 'Low') AS severity
FROM (
    SELECT labels['path'] AS path,
           maxIf(value, labels['kind'] = 'available') AS available,
           maxIf(value, labels['kind'] = 'size') AS size
    FROM datum.latest_value(metric = 'atlas_filesystem_bytes', window = 600)
    GROUP BY path
)
WHERE size > 0 AND available < 0.25 * size
