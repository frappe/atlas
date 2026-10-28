# Datum telemetry

## Contents

- Tables and resources
- Rules
- Metric map
- SQL patterns

## Tables and resources

- `samples(ts, metric, resource_id, labels, value)`: metrics. Read a label with `labels['interface']`.
- `logs(ts, resource_id, product, service, level, source, message, attributes)`: log lines at info and above.

| Metric prefix | `resource_id` |
| --- | --- |
| `system_`, `metal_` | Metal Server name, such as `01a0cb6d-7f90-...`. Get it from `describe_host`. |
| `service_` | Service node name, such as `proxy-012` or `wireguard-001`. |
| `atlas_` | `atlas-<region>`. A `server` label names the host. |

Data is kept for about 14 days.

## Rules

- Always filter `ts`. Select only the columns you need.
- A `_total` metric is a counter. Use `get_counter_increase`, never its value. The tool counts a drop as a restart.
- Group by `resource_id, metric, labels`. Never compare or subtract two different metrics.
- Compare with a normal window: the same hour yesterday, or the 6 hours before the problem.
- Use `toStartOfInterval(ts, INTERVAL 5 MINUTE)` for a trend. Return at most about 50 rows.

## Metric map

Use utilization, saturation, and errors for each host resource. Use rate, errors, and duration for each service.

| Area | Utilization | Saturation | Errors |
| --- | --- | --- | --- |
| CPU | `system_cpu_usage_percent` | `system_load_average` against `system_cpu_count`, `system_pressure_stall_seconds_total{resource=cpu}`, `system_cpu_core_throttles_total` | |
| Memory | `system_memory_usage_percent` | `system_pressure_stall_seconds_total{resource=memory}`, `system_memory_swap_used_bytes` | `system_vmstat{field=oom_kill}`, `system_edac_uncorrectable_errors_total` |
| Disk | `system_disk_io_time_seconds_total`, `system_zfs_pool_capacity_percent` | `system_disk_io_in_progress`, `system_pressure_stall_seconds_total{resource=io}`, `system_zfs_device_slow_ios` | `system_zfs_device_errors`, `system_zfs_pool_data_errors`, `system_disk_scsi_timeouts_total`, `system_smart_healthy`, `system_md_degraded`, `system_filesystem_read_only` |
| Network | `system_network_receive_bytes_total`, `system_network_transmit_bytes_total` | `system_network_softnet_dropped_total`, `system_network_conntrack_entries` against `_limit` | `system_network_receive_errors_total`, `system_network_transmit_errors_total`, `_drops_total`, `system_network_carrier_changes_total`, `system_network_up` |

| Service | Metrics |
| --- | --- |
| metald | `metal_api_requests_total{status}`, `metal_api_request_duration_seconds_total`, `metal_reconciler_failures_total{loop}`, `metal_reconciler_last_pass_timestamp_seconds{loop}`, `metal_telemetry_pushes_total{result}` |
| VMs on a host | `metal_vm_count{state}`, `metal_vm_unavailable{reason}`, `metal_vm_failing`, `metal_vm_boot_unreachable_total{kind}`, `metal_vm_operations_total{phase,result}`, `metal_vm_operations_in_progress{phase}` |
| Migration | `metal_migration_sources`, `metal_migration_destinations{state}`, `atlas_migration_oldest_active_seconds` |
| Atlas app | `atlas_error_logs_last_minute`, `atlas_job_failed{queue}`, `atlas_job_queue_length{queue}`, `atlas_host_sync_age_seconds{server}`, `atlas_metal_requests_total{status}` |
| HTTP proxy | `service_proxy_requests_total{status_class}`, `service_proxy_slow_requests_total`, `service_proxy_upstream_seconds_total` |
| Systemd | `system_systemd_failed_units`, `system_systemd_unit_restarts_total{unit}` |

## SQL patterns

Counter increase in the last hour, when `get_counter_increase` does not fit:

```sql
SELECT resource_id, metric, labels, max(value) - min(value) AS increase
FROM samples
WHERE metric IN ('system_network_receive_errors_total', 'system_network_transmit_errors_total')
  AND ts > now() - INTERVAL 1 HOUR
GROUP BY resource_id, metric, labels
HAVING increase != 0
```

Now against the same hour yesterday:

```sql
SELECT resource_id, ts > now() - INTERVAL 1 HOUR AS is_now, avg(value) AS average, max(value) AS peak
FROM samples
WHERE metric = 'system_cpu_usage_percent'
  AND (ts > now() - INTERVAL 1 HOUR OR ts BETWEEN now() - INTERVAL 25 HOUR AND now() - INTERVAL 24 HOUR)
GROUP BY resource_id, is_now
```

What changed (versions, restarts, reboots):

```sql
SELECT resource_id, metric, labels, min(value) AS first, max(value) AS last
FROM samples
WHERE metric IN ('metal_build_info', 'metal_process_start_time_seconds', 'system_boot_time_seconds')
  AND ts > now() - INTERVAL 1 DAY
GROUP BY resource_id, metric, labels
```

More than one `labels` row for `metal_build_info` means a new version. `first != last` for a start or boot time means a restart or a reboot.

Errors and warnings of one host:

```sql
SELECT ts, service, level, message
FROM logs
WHERE resource_id = '<Metal Server name>' AND level IN ('error', 'warn') AND ts > now() - INTERVAL 1 HOUR
ORDER BY ts DESC
LIMIT 50
```
