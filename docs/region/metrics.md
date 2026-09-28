# Host and VM metrics

Atlas reads current VM metrics from Metal. Metal can also export numeric host and VM samples to Datum for storage in ClickHouse. VM control does not require successful telemetry delivery.

## Configuration

Set `atlas_datum_url` in the Atlas site configuration to the reachable Datum base URL. Run the site migration before installing the changed code so the Metal Server token fields exist. Run host installation to write the endpoint into `/var/lib/metal/metald.toml` and install the host's token bundle. Replacing the daemon binary alone does not update configuration.

```json
{"atlas_datum_url": "https://datum.example.com"}
```

The installer writes `[datum].url`, including an empty value when export is disabled, and preserves other Datum settings. Metal reads the token bundle on each export pass. The host must trust the endpoint's TLS certificate and reach Atlas's published regional keys through Datum's configured key retrieval path.

## Metric meanings

| Value | Meaning and lifetime |
| --- | --- |
| CPU | Cumulative microseconds charged to the current Firecracker process's cgroup. Stopped guests report zero. |
| Memory | Bytes currently charged to the Firecracker cgroup, including charged cache and runtime overhead. Stopped guests report zero. |
| Disk | Size and used space in MiB, as of the last reconciliation pass. |
| Network | Cumulative unicast IPv4/IPv6 packets and bytes at the monitored TAP, from the guest's perspective. These are attachment counters, not billing totals or complete Ethernet traffic counts. |
| VM up | One for observed running or paused state; zero otherwise. |

Network counters survive idle sleep and Stop/Start while the traffic attachment remains. Detaching or recreating the attachment, moving to another host, or restarting Metal resets them. Rate calculations must handle counter resets. Bytes and packets are sampled separately and can advance between reads.

## Token delivery

Atlas issues one host token and one token per assigned VM. A bundle shares one issuance timestamp and a one-hour lifetime. Stored expiry matches the signed expiry and does not extend with SSH delivery time.

A placement change increments the affected hosts' `datum_tokens_revision`, clears their stored expiry, and requests refresh after the transaction commits. Deletion requests removal from the old host's bundle. Failed queue submission is logged, and the database request remains pending. A shipment records expiry only if the revision has not changed during delivery.

The scheduler checks ready hosts every minute. It queues bundles with no stored expiry or with at most 30 minutes remaining, and skips an already queued or running refresh. A change during a shipment remains pending for the next scheduler pass. Queue backlog and host outages can delay delivery beyond that interval.

## Export and failures

Metal starts an export pass every minute and can wake earlier on VM changes. A pass runs at most four resource exports concurrently and waits for them to finish. Each resource has a five-second timeout covering collection and delivery. Missing VM tokens skip that VM until a later bundle arrives. Failed reads or pushes are logged and retried with fresh samples on a later pass.

Four workers bound concurrency, not total pass duration. Many slow resources can still make a pass exceed its normal interval. Samples have no durable local buffer, so outages create gaps. The exporter sends numeric metrics; structured log shipping and flow logging require separate work.

## Validation

Local checks cover token timestamps, transactional placement invalidation, stale shipments, generated configuration, API/client consistency, and exporter scheduling. Linux is required for kernel and host behavior.

From `metal/` on a disposable Linux host, rebuild the eBPF object and run:

```sh
make bpf
go test -race ./...
go vet ./...
sudo -E go test -tags integration -run 'TestTrafficCountersKeepConcurrentPackets|TestMonitorObservesPacketsWithoutAGuest' ./internal/network/traffic
sudo -E go test -tags integration -run TestMetricsKeepsNetworkCountersAcrossStopStart ./internal/vm
```

The concurrency test needs at least two CPUs. The lifecycle regression uses a real traffic attachment and a fake guest runtime. Full acceptance also needs a live guest and the deployed Atlas, Metal, Datum, and ClickHouse path:

1. Install a host through Atlas and confirm real host and VM samples in ClickHouse without manually editing Metal's configuration.
2. Create, move, and delete VMs. Confirm prompt token delivery and correct resource identity.
3. Change placement during token shipment. Confirm that the newer request is retried.
4. Verify refresh across token expiry, failed SSH delivery, and queue delay.
5. Exercise idle sleep/wake, Stop/Start, daemon restart, and host reboot. Confirm documented counter resets and continued export.
6. Interrupt Datum access and restore it. Confirm bounded export work, visible failures, recovery, and unaffected VM control.

Source contracts: [Metal Server](../../atlas/metal_server/SPEC.md), [Datum exporter](../../metal/internal/datum/SPEC.md), and [VM manager](../../metal/internal/vm/SPEC.md).
