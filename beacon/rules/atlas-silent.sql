-- title: Atlas stopped reporting
-- description: Atlas sends no metrics. Other alerts in the region cannot fire.
-- severity: Critical
-- resource_type: Region
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/atlas-silent.md
-- interval: 60
-- pending_for: 60
-- resolve_after: 120
-- Atlas sends atlas_virtual_machine_drafts every minute, even when it has no host or draft.
SELECT {region:String} AS resource_id, '' AS alert_key, toFloat64(dateDiff('second', last_ts, now())) AS value,
       concat('no Atlas sample for ', toString(value), ' s (limit 180 s)') AS detail
FROM (
    SELECT max(ts) AS last_ts
    FROM datum.samples_recent
    WHERE metric = 'atlas_virtual_machine_drafts' AND resource_id = 'atlas' AND ts > now() - INTERVAL 1 DAY
)
WHERE value >= 180
