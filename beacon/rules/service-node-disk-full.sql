-- title: Service node disk almost full
-- severity: High
-- resource_type: Server
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/service-node-disk-full.md
-- interval: 300
-- pending_for: 300
-- resolve_after: 900
-- Under 10% free pages. Under 25% free is Low: plan space before it fills.
SELECT resource_id, mountpoint AS alert_key, free AS value,
       concat(mountpoint, ' has ', toString(round(free, 1)), '% free') AS detail,
       if(free < 10, 'High', 'Low') AS severity
FROM (
    SELECT resource_id, labels['mountpoint'] AS mountpoint,
           argMaxIf(value, ts, metric = 'service_filesystem_free_bytes') / nullIf(argMaxIf(value, ts, metric = 'service_filesystem_total_bytes'), 0) * 100 AS free
    FROM datum.samples_recent
    WHERE metric IN ('service_filesystem_free_bytes', 'service_filesystem_total_bytes') AND labels['type'] != 'zfs' AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY resource_id, mountpoint
    HAVING countIf(metric = 'service_filesystem_free_bytes') > 0
)
WHERE free < 25
