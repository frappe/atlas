-- title: Storage pool almost full
-- description: A ZFS pool is nearly full. ZFS slows down and VM disks can stop writing.
-- severity: High
-- resource_type: Metal Host
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/host-pool-capacity.md
-- charts: host-zfs-pool
-- interval: 300
-- pending_for: 600
-- resolve_after: 600
-- Over 90% used pages, because ZFS slows down and VM disks can stop writing. Over 80% is Low.
SELECT resource_id, labels['pool'] AS alert_key, value,
       concat('pool ', alert_key, ' is ', toString(round(value)), '% used (limit 90%)') AS detail,
       if(value > 90, 'High', 'Low') AS severity
FROM datum.latest_value(metric = 'system_zfs_pool_capacity_percent', window = 900)
WHERE value > 80
