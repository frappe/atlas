-- title: Processes killed for lack of memory
-- description: The kernel killed processes because the machine ran out of memory. A service or a VM can be down.
-- severity: Medium
-- resource_type: Metal Host, Virtual Machine
-- charts: host-oom-kills, vm-memory, host-memory
-- interval: 300
-- pending_for: 300
-- resolve_after: 1800
-- The kernel counts each OOM kill, also a kill in a memory cgroup. On a host, the killed process can be the process of a VM.
SELECT resource_id, '' AS alert_key, increase AS value,
       concat(toString(increase), ' processes killed for lack of memory in 15 min') AS detail
FROM datum.counter_increase(metric = 'system_vmstat', window = 900)
WHERE labels['field'] = 'oom_kill' AND increase > 0
