-- title: Machine silent
-- severity: Medium
-- resource_type: Machine
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/machine-silent.md
-- interval: 60
-- pending_for: 120
-- resolve_after: 120
-- Each machine sends system_boot_time_seconds at least once a minute: metald on a host, the Datum agent on an Atlas service VM or a Cargo machine, Pilot on a Pilot VM.
-- Atlas lists the machines that must send now as atlas_machine. A machine that sent in the last 14 days and has no sample for 3 minutes is silent, so a VM that never sends telemetry does not alert.
-- host-down covers a host that Atlas cannot reach, and vms-unavailable-on-host a VM that stopped, so silence alone is Medium.
WITH (SELECT max(ts) FROM datum.samples_recent WHERE metric = 'atlas_machine' AND resource_id = 'atlas' AND ts > now() - INTERVAL 10 MINUTE) AS snapshot
SELECT e.machine AS resource_id, '' AS alert_key, toFloat64(if(l.last_ts IS NULL, 3600, dateDiff('second', l.last_ts, now()))) AS value,
       if(l.last_ts IS NULL, 'no machine metrics for over 1 h', concat('no machine metrics for ', toString(round(value / 60)), ' min')) AS detail
FROM (
    SELECT labels['machine'] AS machine
    FROM datum.samples_recent
    WHERE metric = 'atlas_machine' AND resource_id = 'atlas' AND ts = snapshot
) AS e
LEFT JOIN (
    SELECT resource_id, max(ts) AS last_ts
    FROM datum.samples_recent
    WHERE metric = 'system_boot_time_seconds' AND ts > now() - INTERVAL 1 HOUR
    GROUP BY resource_id
) AS l ON l.resource_id = e.machine
WHERE (l.last_ts IS NULL OR l.last_ts < now() - INTERVAL 3 MINUTE)
  AND e.machine IN (SELECT resource_id FROM datum.daily_ingestion_stats WHERE date > today() - 14)
SETTINGS join_use_nulls = 1
