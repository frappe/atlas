-- title: VMs unavailable on host
-- severity: High
-- resource_type: Host
-- runbook_url: https://github.com/frappe/atlas/blob/develop/beacon/runbooks/vms-unavailable-on-host.md
-- interval: 60
-- pending_for: 300
-- resolve_after: 120
-- scope_includes_descendants: 1
SELECT w.resource_id AS resource_id, '' AS alert_key, u.unavailable AS value,
       concat(toString(u.unavailable), ' of ', toString(w.wanted), ' wanted VMs unavailable') AS detail
FROM (
    SELECT resource_id, sum(value) AS wanted
    FROM datum.latest_value(metric = 'metal_vm_wanted_running', window = 300)
    GROUP BY resource_id
) AS w
INNER JOIN (
    SELECT resource_id, sum(value) AS unavailable
    FROM datum.latest_value(metric = 'metal_vm_unavailable', window = 300)
    GROUP BY resource_id
) AS u ON u.resource_id = w.resource_id
WHERE u.unavailable >= 3 AND u.unavailable >= 0.2 * w.wanted
