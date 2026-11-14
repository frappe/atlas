-- title: Atlas work stuck
-- severity: Medium
-- resource_type: Atlas
-- interval: 300
-- pending_for: 300
-- resolve_after: 600
-- A VM migration that runs for 1 hour, or a draft VM that waits for 1 hour, did not finish.
SELECT 'atlas' AS resource_id, 'migration' AS alert_key, max(value) AS value,
       concat('a VM migration is running for ', toString(round(value / 60)), ' min') AS detail
FROM datum.latest_value(metric = 'atlas_migration_oldest_active_seconds', window = 600)
HAVING value > 3600
UNION ALL
SELECT 'atlas', 'draft', max(value) AS value,
       concat('a draft VM is waiting for ', toString(round(value / 60)), ' min')
FROM datum.latest_value(metric = 'atlas_virtual_machine_oldest_draft_seconds', window = 600)
HAVING value > 3600
