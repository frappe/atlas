-- title: Host storage failed
-- severity: High
-- resource_type: Host
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/host-storage-failed.md
-- interval: 60
-- pending_for: 60
-- resolve_after: 600
-- Each row is one pool, array or mount that can lose data or stop writes now.
SELECT resource_id, alert_key, max(value) AS value, any(detail) AS detail
FROM (
    SELECT resource_id, concat('zfs-pool:', labels['pool']) AS alert_key, value,
           concat('ZFS pool ', labels['pool'], ' is not healthy') AS detail
    FROM datum.latest_value(metric = 'system_zfs_pool_healthy', window = 300)
    WHERE value = 0
    UNION ALL
    SELECT resource_id, concat('zfs-errors:', labels['pool']), value,
           concat('ZFS pool ', labels['pool'], ' has ', toString(value), ' data errors')
    FROM datum.latest_value(metric = 'system_zfs_pool_data_errors', window = 300)
    WHERE value > 0
    UNION ALL
    SELECT resource_id, concat('raid:', labels['device']), value, concat('RAID array ', labels['device'], ' is degraded')
    FROM datum.latest_value(metric = 'system_md_degraded', window = 300)
    WHERE value > 0
    UNION ALL
    SELECT resource_id, concat('read-only:', labels['mountpoint']), value, concat(labels['mountpoint'], ' is read-only')
    FROM datum.latest_value(metric = 'system_filesystem_read_only', window = 300)
    WHERE value > 0
)
GROUP BY resource_id, alert_key
