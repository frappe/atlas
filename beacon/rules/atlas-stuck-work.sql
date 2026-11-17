-- title: VM migration or setup too slow
-- description: A VM migration or a new VM has waited for over an hour. The work did not finish.
-- severity: Medium
-- resource_type: Atlas
-- charts: atlas-stuck-work
-- interval: 300
-- pending_for: 300
-- resolve_after: 600
-- A VM migration that runs for 1 hour, or a draft VM that waits for 1 hour, did not finish.
SELECT 'atlas' AS resource_id, kind AS alert_key, age AS value,
       concat(if(kind = 'migration', 'a VM migration is running for ', 'a new VM is waiting for '), toString(round(age / 60)), ' min (limit 60 min)') AS detail
FROM (
    SELECT kind, max(value) AS age
    FROM (
        SELECT 'migration' AS kind, value FROM datum.latest_value(metric = 'atlas_migration_oldest_active_seconds', window = 600)
        WHERE resource_id = 'atlas'
        UNION ALL
        SELECT 'draft', value FROM datum.latest_value(metric = 'atlas_virtual_machine_oldest_draft_seconds', window = 600)
        WHERE resource_id = 'atlas'
    )
    GROUP BY kind
)
WHERE age > 3600
