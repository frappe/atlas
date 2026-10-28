# Playbooks

Find the symptom. Use its checks as hypotheses to test. Metric names are in `telemetry.md`.

## Contents

- VM is unreachable, does not boot, or stops
- Host is slow or overloaded
- Disk, ZFS pool, or RAID
- Network, NIC errors, or WireGuard
- metald errors or a stuck operation
- Migration is stuck
- Atlas errors or failed jobs
- Site is down or slow through the HTTP proxy
- Datum has no data for a host
- An alert looks wrong

## VM is unreachable, does not boot, or stops

1. `describe_virtual_machine`: state, host, and the last operation.
2. `metal_vm_unavailable{reason}`, `metal_vm_failing`, and `metal_vm_boot_unreachable_total{kind}` on its host.
3. `read_firecracker_log`, then `read_virtual_machine_journal`.

Known cause: the Firecracker API stops, but the process stays alive. metald then reports the VM as running. The Firecracker log shows the API error, and the VM journal shows no exit.

## Host is slow or overloaded

1. Check utilization, saturation, and errors for CPU, memory, disk, and network on the host.
2. Compare with the same hour yesterday. Load average above `system_cpu_count` is saturation.
3. `metal_vm_count{state}` and `atlas_host_cpu_millicores` show if the host has too many VMs.
4. `check_host_health` when Datum data is old.

## Disk, ZFS pool, or RAID

1. `system_zfs_pool_healthy`, `system_zfs_pool_capacity_percent`, `system_zfs_device_errors`, and `system_zfs_device_slow_ios`.
2. `system_md_degraded`, `system_smart_healthy`, and `system_disk_scsi_timeouts_total` for the physical disk.
3. `system_filesystem_read_only` for a file system that went read-only.
4. `check_host_health` for live `zpool status`.

## Network, NIC errors, or WireGuard

1. `get_counter_increase` for each error and drop counter. Do not compare receive errors with transmit errors.
2. `system_network_carrier_changes_total` and `system_network_up` show a link that went down.
3. `system_network_conntrack_entries` near `system_network_conntrack_entries_limit` drops new connections.
4. `check_host_health` shows the WireGuard handshake age of each peer.

## metald errors or a stuck operation

1. `get_reconciler_status`: a loop with `is_stale` true is stuck. Failures that grow show a loop that fails each pass.
2. VM operations that grow each pass with no new request show a loop that never converges.
3. `metal_vm_operations_in_progress{phase}` and `metal_vm_oldest_pending_seconds` show a stuck operation.
4. Datum logs with `service = 'metald'`, then `read_metald_journal` for lines below warning.

## Migration is stuck

1. `atlas_migration_oldest_active_seconds` for the age.
2. `metal_migration_sources` on the source and `metal_migration_destinations{state}` on the destination.
3. `describe_virtual_machine` for the migration record. Then read the metald journal on both hosts.

## Atlas errors or failed jobs

1. `atlas_error_logs_last_minute`, `atlas_job_failed{queue}`, and `atlas_job_queue_length{queue}`.
2. `list_recent_errors`, then `read_error_log` for the first error, not the most frequent one.
3. `atlas_host_sync_age_seconds{server}` shows a host that Atlas cannot reach.
4. `atlas_metal_requests_total{status}` shows failed calls from Atlas to metald.

## Site is down or slow through the HTTP proxy

1. Rate, errors, and duration: `service_proxy_requests_total{status_class}`, `service_proxy_slow_requests_total`, and `service_proxy_upstream_seconds_total`.
2. Datum logs with `service = 'openresty'`. `no host in upstream` means that the proxy has no route for the site.
3. Then check the VM behind the site.

## Datum has no data for a host

1. `get_telemetry_freshness` shows the age.
2. `metal_telemetry_pushes_total{result}` and `metal_telemetry_collector_errors_total` before the gap.
3. `check_host_health` and `read_metald_journal` over SSH.

## An alert looks wrong

1. Run the alert query with `run_saved_query`.
2. Get the raw samples behind the value.
3. If the raw data does not agree with the alert, report the alert query as the cause. Name the query key and the wrong part of the query, such as a missing `GROUP BY` column.
