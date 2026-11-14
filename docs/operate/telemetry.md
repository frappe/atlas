# Telemetry

Atlas, Metal hosts, and service nodes send metrics and logs to regional [Datum](https://github.com/frappe/Datum), which stores them in ClickHouse. Read them with SQL or the [Insights dashboards](dashboards.md).

```text
Metal host
  metald ── machine, metald, VM, and migration metrics ──> Datum /v1/ingest
  journald ── Datum agent ──> Datum /v1/logs/ingest
Atlas
  scheduled job ── regional state, counters, events ──> Datum /v1/ingest
  scheduled job ── Error Logs ──> Datum /v1/logs/ingest
  BMC jobs ── Redfish readings and events of each host ──> Datum, as the host
Proxy, WireGuard gateway, and IPv6 router VM
  Datum agent ── machine metrics, role metrics ──> Datum /v1/ingest/remote
  Datum agent ── journald and nginx error lines ──> Datum /v1/logs/ingest
```

During a Datum outage, metald retains counters and up to 50,000 events in memory. Atlas uses Redis. Vector buffers up to 256 MiB of logs on disk.

## Configuration

| Setting | Owner | Purpose |
| --- | --- | --- |
| `datum_url` | Site config | Datum base URL. Without it, Atlas pushes nothing and installs no host telemetry. |
| `[telemetry]` in `metald.toml` | Atlas, host | Atlas writes `datum_url` and `datum_token`. metald rereads them every 24 hours, or when the token expires. The host sets `interval` (default `60s`) and `restart_count_units`, the systemd units whose restart count it reports. |
| `/etc/vector/datum.d/`, `/etc/vector/datum.env` | The Datum agent | The configuration and the token. Readable only by root and Vector. |

Datum names each row by the `resource_id` in its token, as the telemetry contract sets. A host uses its Metal Server name. A VM uses its Virtual Machine name, also when it runs a service node: `atlas_service_node{node, virtual_machine}` maps the two. Atlas uses `atlas`. The `atlas_metal_server_info` metric maps a Metal Server name to its title.

## Tokens and installation

Atlas signs every token with its regional key for the `atlas-datum:<region ID>` audience.

- Atlas signs a new 5-minute token for each of its own pushes.
- A host token lasts 7 days. `renew_telemetry_credentials` reinstalls host telemetry with a new token every day at 03:30.
- A host gets telemetry right after Atlas installs metald on it, during provisioning or **Re-configure Metald**.
- To repair one host, run **Configure Telemetry** (`configure_telemetry`) on its Metal Server.

The SSH Task `install-host-monitoring.sh` installs smartmontools, and makes the RAID monitor `mdmonitor` log array events to the journal. Then, over direct SSH so that no SSH Task stores a token:

1. `configure-metald-telemetry.sh` writes the token to `metald.toml`.
2. The Datum agent installs with `PRODUCT=metal`, no machine metrics, because metald sends them, and the `info` lines of `metal.service`. When metald listens on `127.0.0.1:9003`, the agent writes through that proxy.

The Datum agent is the one Vector configuration of the telemetry contract. Datum owns it in `datum_client/agent/`, and `DatumAgent` (`atlas/atlas/core/telemetry/agent.py`) installs it. See the contract in Beacon `docs/telemetry.md`.

## Service node telemetry

The VM of each active proxy, WireGuard gateway, and IPv6 router node, and the Cargo VM, run the Datum agent, under the name of the VM. The Cargo VM sends with product `cargo` and no role file. Pilot runs the Cargo site on it and sends only its own metrics.

- A node and the Cargo VM get telemetry when their provisioning completes. `renew_service_telemetry_credentials` installs it again with a new 7-day token every day at 03:30.
- The agent adds the role file of the service from `atlas/atlas/core/telemetry/agent_roles/`.
- Proxy: the agent reads the JSON nginx access log and sends only counts and times: `proxy_requests_total` by status class and origin (`proxy` when the last upstream sent no response header, so the proxy made the answer, `upstream` when the site answered, also with its own 502 or 504), `proxy_request_seconds_total`, `proxy_upstream_seconds_total`, and `proxy_slow_requests_total` for requests over 1 second. Access lines stay on the node, because they hold client addresses. nginx error lines at `error` or worse go to the logs, except the upstream errors (refused, reset, timed out) that the site and proxy checks cover.
- IPv6 router: `ipv6_router_program_attached` is 1 while the tc eBPF program is attached to `eth0`.
- WireGuard gateway: `wireguard_peers` and `wireguard_peers_active`, the peers with a handshake in the last 3 minutes. A root timer of the gateway writes the counts, because reading WireGuard needs root.

To repair one node or the Cargo VM, run **Configure Telemetry** (`configure_telemetry`) on its form.

## Metrics

Counters end in `_total`. Metal counters reset on process restart; Atlas counters remain in Redis. Observations record individual events for exact percentiles.

Put VM, request, and migration IDs in logs. The `atlas_service_node` and `atlas_service_ready` metrics are exceptions: they identify the few service VMs.

| Prefix | Source | Reports |
| --- | --- | --- |
| `system_*` | Host collectors | CPU, memory, kernel, storage, RAID, network, sensors, disk health, clock, systemd, and ZFS |
| `metal_vm_*` | VM manager and runtime | Counts, allocations, actions, pending changes, boots, and availability |
| `metal_api_*` | Metal API | Request counts and mutation latency |
| `metal_reconciler_*` | Reconcile loops | Pass counts, duration, last completion, and failures |
| `metal_migration_*` | Destination host | State transitions, duration, downtime, and transfer size |
| `metal_image_*`, `metal_snapshot_*` | Storage | Image import, snapshot creation, and snapshot upload duration and size |
| `metal_process_*`, `metal_telemetry_*` | metald | Start time, memory, goroutines, pushes, dropped events |

| `atlas_*` | Atlas, as resource `atlas` | Regional inventory, the machines that must send (`atlas_machine`), capacity, migrations, placement, API calls, queues, public IPs, images, service readiness, the free space of its own root, bench, and MariaDB file systems, and the days left on the wildcard and metald TLS certificates |
| `system_*` | The Datum agent on service VMs and the Cargo VM | The machine metrics of the contract |
| `proxy_*`, `ipv6_router_*`, `wireguard_*` | The Datum agent on service VMs | The role metrics above |
| `bmc_*` | Atlas, from the host BMC | Out-of-band health, sensors, and power. See [BMC hardware](#bmc-hardware). |

metald sends CPU metrics as totals, so a host with many CPUs sends the same number of series as a small host. `system_cpu_seconds_total` has `cpu="all"`. The scheduler and softnet counters have no `cpu` label. The frequency and the thermal throttles have a `package` label. No reader needs the history of one CPU: read it on the host in `/proc/stat`, `/proc/schedstat`, `/proc/net/softnet_stat` and `/sys/devices/system/cpu/`. Of each hwmon sensor, metald sends only the input and the alarm and fault flags.

Kernel tables use a `field` label, for example `system_memory_info{field="MemAvailable"}`. `system_netstat` and `system_vmstat` send only the fields that the host charts read, listed in `netstatFields` and `vmstatFields`. Add a field there when a chart or rule needs it.

Collectors exclude VM volumes, interfaces, units, and per-dataset ZFS counters. Datum keeps samples 90 days, so temporary resources would cause many series.

## Boot time

`metal_vm_boot_seconds` has 2 stages for each cold, warm, and restore boot. `started` is the time until Firecracker runs the guest. `reachable` is the time until the guest answers ping from its network namespace. metald pings every 250 ms for up to 3 minutes. A guest that does not answer counts in `metal_vm_boot_unreachable_total`.

## VM availability

metald counts the VMs that the user wants running in `metal_vm_wanted_running`. A VM that the user stopped, or a Sleepy VM that sleeps, is not wanted running. A change that the user asked for, such as a restart, does not count for its first 5 minutes.

`metal_vm_unavailable{reason}` counts unavailable VMs by reason:

| Reason | Meaning |
| --- | --- |
| `not_running` | The applied state is not running, for example after a crash. |
| `stuck` | A change is still not applied after 5 minutes. |
| `unreachable` | The guest missed 3 consecutive pings. |

metald pings each running guest every minute from its network namespace.

metald logs `virtual machine unavailable` with the reason when a VM becomes unavailable, and `virtual machine available again` with the duration when it recovers.

## SLOs

Each service level objective (SLO) requires 99% of events within target over 28 days. See [dashboards](dashboards.md#sla-and-slos) for uptime and error budgets.

| SLO | Target | Events |
| --- | --- | --- |
| Create, Resize | 60 s | `metal_vm_action_duration_seconds`, from the Metal API write to the applied state |
| Start, Terminate | 30 s | same |
| Stop | 15 s | same |
| Snapshot | 15 min | `atlas_image_create_duration_seconds`, from the image request to Available. A failed image misses. |
| Cold boot to ping | 60 s | `metal_vm_boot_seconds{kind="cold",stage="reachable"}`. A boot without an answer misses. |
| Warm boot to ping | 20 s | same, `kind="warm"` |
| Migration | 10 min | `atlas_migration_duration_seconds`. A failed migration misses. An aborted one does not count. |
| Migration downtime | 60 s | `metal_migration_downtime_seconds` |

## Disk health

These metrics show a failing disk before a pool or array loses data.

| Metric | Bad value |
| --- | --- |
| `system_smart_healthy` | `0`: the disk reports that it fails its own health check. |
| `system_smart_exit_status` | Bit 3 (8) a failing disk, bit 4 (16) an attribute at its threshold, bit 6 (64) errors in the error log, bit 7 (128) a failed self-test. |
| `system_smart_attribute{field="raw"}` for `id` 5 and 187 | An increase: new reallocated or uncorrectable sectors. A stable count is old wear. |
| `system_smart_attribute{field="raw"}` for `id` 197 and 198 | More than `0`: pending or offline-uncorrectable sectors. |
| `system_nvme_smart{field="critical_warning"}` | More than `0`. |
| `system_nvme_smart{field="media_errors"}` | An increase. |
| `system_disk_scsi_timeouts_total` | An increase: a command to a SATA or SAS disk timed out. |
| `system_smart_scsi_grown_defects`, `system_smart_scsi_uncorrected_errors{operation}` | An increase in 1 day: a SAS disk fails. |
| `system_md_degraded`, `system_md_mismatch_cnt` | More than `0`: a missing member, or copies that differ after a check. |
| `system_md_member_corrected_errors` | An increase: md rewrote a sector that a member could not read. |
| `system_zfs_device_errors{kind}`, `system_zfs_device_slow_ios` | An increase on one device. |
| `system_zfs_pool_data_errors` | More than `0`: data that no copy could repair. |
| `system_zfs_scan_end_timestamp_seconds`, `system_zfs_scan_errors` | A scrub older than 35 days, or a scrub with errors. |

metald runs `smartctl` every 10 minutes, so SMART metrics have one sample for each 10 minutes. It does not wake a disk in standby. NVMe disks are read directly, without `smartctl`.

## Logs

The Datum agent reads the journal and posts batches of up to 5,000 lines, 10 requests a minute at most. `service` is the unit name without `.service`.

| `service` | Lines |
| --- | --- |
| `metal` | Every metald line at `info` or worse. Its JSON fields become `attributes`. A Go crash line (`panic:` or `fatal error:`) has level `critical`, so it shows on Atlas - Errors. |
| `kernel` | Kernel warnings and errors, such as NIC, OOM, and ZFS messages. |
| unit name | Warnings and errors of every other unit. |

metald logs each completed reconcile step at info level. The steps that every pass runs for every VM (`network`, `inspect`, and `storage`) log at debug level when they succeed.

## Atlas errors

`publish_error_logs` sends each new Atlas Error Log to Datum every minute, up to 1,000 for each run. Each line has `service` `atlas` and `source` `error_log`. The message is the Error Log title.

| Attribute | Value |
| --- | --- |
| `fingerprint` | Frappe's fingerprint of the error. Errors with the same fingerprint have the same cause. |
| `exception` | The exception class, such as `frappe.exceptions.PermissionError`. |
| `unhandled` | `1` for a request or job that failed with an exception that no Atlas code caught, `0` for an error that Atlas logs on purpose. |
| `request_id` | The Frappe trace ID. It is the same ID that metald logs for the request. |
| `kind`, `path` | `http_request` and the path, or `background_job` and the job method. |
| `traceback` | The last 1 KiB of the traceback. Form data is not sent. |

The job keeps its position in Redis. If the position is lost, it starts again 5 minutes back.

## Beacon resources

Beacon, the incident manager, reads `GET /api/atlas/beacon/resources` to know what this region runs. Only Central identities can call it. It returns Atlas itself (`atlas`), each host, each virtual machine with its host as parent, each proxy, WireGuard gateway and IPv6 router node with its virtual machine as parent, and the Cargo server (`cargo`). Each status maps to `Pending`, `Active`, `Suspended` or `Terminated`. A virtual machine that sleeps is `Suspended`. Any other stopped virtual machine stays `Active`, so a crashed virtual machine keeps its alerts. The response has an ETag, and a request with a matching `If-None-Match` gets `304`.

## Beacon rules

Beacon reads its alert rules, services and runbooks for Atlas, metald, hosts and the regional services from `beacon/` in this repository. `beacon/rules/<rule-key>.sql` is one alert rule, `beacon/services/<service>.sql` is one SLO service, and `beacon/runbooks/<rule-key>.md` tells the responder what to do. Change a rule in the same pull request as the metric that it reads. Beacon applies the change when staff click Sync Definitions in Beacon Settings.

## BMC hardware

Atlas reads each host's baseboard management controller (BMC) over Redfish without access to the host OS. It sends hardware health, sensors, power, and events to Datum under the host's identity.

Set the provider to **Generic**, the BMC driver to **Redfish**, and each Metal Server's BMC URL and credentials. Every 5 minutes, Atlas queues up to 5 hosts per job on the long queue. It skips deleted hosts and jobs already queued or running.

A job reads hosts in sequence. Each read has a 10-second timeout within a 90-second budget per host. Section failures leave partial metrics.

| Metric | Labels | Value |
| --- | --- | --- |
| `bmc_up` | | `1` if the system read succeeds, otherwise `0` |
| `bmc_system_info` | `manufacturer`, `model`, `serial`, `bios_version`, `bmc_firmware`, `processor_model`, `processor_count`, `memory_gib`, `power_state` | `1` |
| `bmc_health` | `component`: `system`, `chassis`, or `manager` | `0` OK, `1` Warning, `2` Critical |
| `bmc_component_health` | `kind` (`memory`, `processor`, `fan`, `power_supply`), `name` | same |
| `bmc_temperature_celsius`, `bmc_temperature_critical_celsius` | `name` | Reading and critical threshold |
| `bmc_fan_speed` | `name`, `unit` (`RPM` or `Percent`) | Speed |
| `bmc_power_watts` | `name`, `kind` (`consumed`, `average`, `maximum`) | Watts |
| `bmc_power_supply_input_volts`, `bmc_power_supply_output_watts` | `name` | Volts and watts |
| `bmc_voltage_volts` | `name` | Volts |
| `bmc_clock_offset_seconds` | | BMC clock minus Atlas clock |

Atlas sends new or updated Warning and Critical entries with `source=bmc` and level `warning` or `critical`. Attributes are `log`, `entry_id`, `message_id`, `sensor_type`, `entry_type`, and `created`, when present. Atlas skips entries whose origin is another log to avoid copies.

Atlas records sent entry IDs and timestamps in Redis after Datum accepts the data. The first read sends only entries from the last 30 days. Atlas skips collections with more than 1,024 members.

Atlas adjusts event times for the BMC clock offset and keeps the original timestamp in `created`. Later reads use the read time if the timestamp is missing, over 30 days old, or over 5 minutes ahead.

## Follow one request

Atlas sends its Frappe request ID as `X-Request-ID`. metald logs it on the API request line and on the `virtual machine action completed` line that finishes the change.

```sql
SELECT ts, resource_id, message, attributes
FROM datum.logs
WHERE attributes['request_id'] = '<request ID>' OR attributes['virtual_machine_id'] = '<VM ID>'
ORDER BY ts
```

The action line has the `action`, the `generation`, and the `duration` from the API write to the applied state. After a metald restart, an action line has no `request_id`. Match it on `virtual_machine_id` and `generation`.

## Check a host

1. Run `systemctl status vector` and `journalctl -u vector --since "15 minutes ago"`.
2. Run `journalctl -u metal.service | grep telemetry`. metald logs the first failed push and the recovery.
3. Check that `[telemetry]` in `/var/lib/metal/metald.toml` has `datum_url` and `datum_token`. Without them, metald logs `telemetry has no Datum credentials`.

A `429` from Datum means the caller sent more than 12 requests a minute to one route. Vector retries it.

::: details Source code and tests

- [metald telemetry package](../../metal/internal/telemetry/SPEC.md) owns the registry, the push, and the host collector.
- [Atlas telemetry](../../atlas/atlas/core/telemetry/) owns Atlas metrics and tokens.
- [install-host-monitoring.sh](../../atlas/scripts/install-host-monitoring.sh) installs smartmontools and RAID logging.
- [configure-metald-telemetry.sh](../../atlas/scripts/configure-metald-telemetry.sh) writes the token of metald.
- [agent.py](../../atlas/atlas/core/telemetry/agent.py) installs the Datum agent.

:::
