-- title: Machine disk almost full
-- severity: High
-- resource_type: Machine
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/machine-disk-full.md
-- interval: 300
-- pending_for: 300
-- resolve_after: 900
-- Every machine sends its file systems. Under 10% free pages. Under 25% free is Low: plan space before it fills.
-- The disk of a tenant VM is the customer's: Pilot tells the customer, so a tenant VM does not alert.
-- A ZFS file system shares the space of its pool, so host-pool-capacity covers it.
SELECT resource_id, mountpoint AS alert_key, free AS value,
       concat(mountpoint, ' has ', toString(round(free, 1)), '% free') AS detail,
       if(free < 10, 'High', 'Low') AS severity
FROM (
    SELECT resource_id, labels['mountpoint'] AS mountpoint,
           argMaxIf(value, ts, metric = 'system_filesystem_available_bytes') / nullIf(argMaxIf(value, ts, metric = 'system_filesystem_size_bytes'), 0) * 100 AS free
    FROM datum.samples_recent
    WHERE metric IN ('system_filesystem_available_bytes', 'system_filesystem_size_bytes') AND labels['type'] != 'zfs' AND ts > now() - INTERVAL 5 MINUTE
    GROUP BY resource_id, mountpoint
    HAVING countIf(metric = 'system_filesystem_available_bytes') > 0
)
WHERE free < 25
  AND resource_id NOT IN (
      SELECT labels['machine']
      FROM datum.samples_recent
      WHERE metric = 'atlas_machine' AND resource_id = 'atlas' AND labels['kind'] = 'tenant' AND ts > now() - INTERVAL 10 MINUTE
  )
