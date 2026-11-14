-- title: Host pool almost full
-- severity: Medium
-- resource_type: Host
-- interval: 300
-- pending_for: 600
-- resolve_after: 600
SELECT resource_id, labels['pool'] AS alert_key, value,
       concat('pool ', alert_key, ' is ', toString(round(value)), '% used') AS detail
FROM datum.latest_value(metric = 'system_zfs_pool_capacity_percent', window = 900)
WHERE value > 90
