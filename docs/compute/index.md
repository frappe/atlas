# Create and manage a VM

Atlas chooses a host and stores the VM request. Metal applies that request on the host. Start with [how one VM request works](../start/how-a-vm-request-works.md) to learn which component owns each state.

## Create a VM

Atlas [chooses a host](placement.md), saves a draft reservation, and asks Metal to create the VM. Metal accepts the request before the guest is ready. Follow the linked request guide for the full create flow and lost-response behavior.

## Change a VM

Atlas sends power, restart, resource, network, key, and metadata changes to the assigned host. Metal reports the applied generation separately from request acceptance.

See [guest metadata](vm-records.md#guest-metadata) for the limits on custom metadata and other MMDS values.

A resize uses the current host if it has enough capacity. Otherwise, Atlas can reserve another host and use [migration](migration.md).

### Set CPU burst

A burstable VM can use CPU above its baseline. Metal measures its average CPU use over its sliding window, as [average CPU use over a window](#average-cpu-use-over-a-window) explains.

| Field | Meaning | Default | Limits |
|---|---|---|---|
| `is_burstable` | The VM can use CPU above its baseline. | `false` | |
| `baseline_cpu_millicores` | The CPU that the VM can always use. | 20% of `cpu_millicores` | 1 to `cpu_millicores` |
| `sliding_window_minutes` | How far back Metal measures average CPU use. | 60 | 1 to 1440 |

Set the fields in the create or resize API request, or with **Actions → Resize VM** in Desk. Atlas fills a default for a value that is unset or `0` on a burstable VM. When a VM stops being burstable, Atlas sets the baseline and the window to `0`.

A CPU burst change applies to a running VM, like the idle sleep delay. A move to another host keeps the settings. Atlas rejects a resize that sets `cpu_millicores` below the baseline. Lower the baseline first.

Placement and host capacity still use the full `cpu_millicores` of a burstable VM.

Upgrade metald on a host before you make its VMs burstable. An older metald ignores the burst fields. Atlas then repairs the VM record from Metal at the next resize.

Read [Metal's requested and applied generations](reconciliation.md) for current progress. The Atlas VM list uses [cached host reports](../region/host-sync.md).

## Read VM metrics

Metal samples each VM every 10 seconds. It records CPU time, memory use, disk size and use, disk read and write rates, configured disk limits, and received and sent network traffic.

Sent traffic includes ICMP, UDP, TCP SYN, and TCP RST packet counters. Disk use is from the last reconcile pass, so it can be older than the sample time. A stopped VM has no current CPU, memory, or disk I/O use.

Memory use is the guest's own report through the Firecracker balloon device. A VM that booted without the balloon device, or that sends no valid report, shows the memory charged to its Firecracker cgroup. That value includes host page cache, so it can be larger than the VM memory.

Disk rates come from the VM systemd cgroup's `io.stat` counters for the root disk device. Metal stores whole read and write bytes per second and milli-IOPS, where 1,000 milli-IOPS is one operation per second.

The first sample after Metal starts, and the first sample after a counter reset, stores zero rates. Configured throughput and IOPS limits are zero when unlimited.

The VM unit template enables `IOAccounting=yes`. Before the template takes effect on an existing running VM, enable accounting on that unit with `systemctl set-property --runtime metal-vm@<vm-id>.service IOAccounting=yes`. If `io.stat` is absent, Metal logs a VM sampling error.

Metal keeps seven days of samples in daily files under `machines/<vm-id>/metrics/` on the VM's current host. An hourly cleanup removes complete expired files, so disk cleanup can lag by less than one day. Deleting or migrating a VM removes its local history. The metrics worker does not collect host usage.

Each JSONL line and metrics API sample stores its `timestamp` as UTC Unix seconds.

Read samples with `GET /api/atlas/virtual-machines/<vm-id>/metrics`. The caller must own the VM. Optional `start` and `end` timestamps select a range within the last seven days. The default range is the last 24 hours. Atlas reads the host only when this endpoint is called.

Optional `max_samples` sets the most samples to return, from 10 to 2000. The default is 300. Metal splits the range into clock-aligned buckets of 10s, 20s, 30s, 1m, 2m, 5m, 10m, 15m, 30m, 1h, 3h, 6h, 12h, or 24h. It uses the smallest bucket that keeps the result within `max_samples`. For example, the default 24-hour range returns 5-minute buckets.

Each sample is the latest sample in its bucket, with disk I/O rates averaged across the bucket. `sample_interval_seconds` is the bucket length. Network and CPU values are cumulative counters; divide the difference between adjacent samples by their elapsed seconds to calculate an average rate.

Network counters count unicast IPv4 and IPv6 packets at the VM TAP. The protocol counters read direct transport headers; IPv6 extension headers and later IPv4 fragments still increase the total packet count but not the protocol count. A Metal restart or TAP reattachment resets network counters, so discard a negative counter difference when calculating rates.

### Average CPU use over a window

`Manager.SlidingWindowUsage` in metald gives the average CPU use of a [burstable VM](#set-cpu-burst) over its `sliding_window_minutes`. The result is in milli-CPUs, where 1,000 milli-CPUs is one CPU busy for the full time. A VM that is not burstable returns `ErrNotBurstable`.

metald keeps one window of samples for each burstable VM in memory, so a call does not read the disk. After the metrics sampler stores a sample, it gives the sample to the VM manager. A sample that the store rejects does not go into memory. One window holds one sample for each 10 seconds, plus one. A 60-minute window holds 361 samples and uses about 14 KB.

The first sweep after metald starts reads the stored samples of each burstable VM one time. A VM that becomes burstable, or that gets a new window, reads them at its next sweep. Until that sweep, the previous window applies for up to 10 seconds. A sweep that runs out of time logs `keep VM CPU samples` and tries again at the next sweep.

metald removes the samples of a VM when the VM stops being burstable, is destroyed, or moves to another host.

Metal adds the CPU time and the elapsed time of each pair of adjacent samples in the window. It divides the CPU time by the elapsed time. It skips a pair when the VM was down at either sample, or when both samples have the same second. Thus down time does not lower the average.

When the CPU counter falls, a restart made a new cgroup that counts from 0. Metal counts the new counter value as the CPU time of that pair. The CPU time that the old cgroup used after the previous sample is lost.

The call returns `ErrNotEnoughSamples` when the window has no usable pair. This includes a VM that has no samples on this host.

The value includes the Firecracker process threads, so it can be a little more than the vCPU count multiplied by 1,000. A paused VM counts as up with almost no CPU use. If a restart occurs inside one 10-second interval and the new counter is already more than the old counter, that pair shows less CPU time than the VM used.

## Terminate a VM

1. Atlas requests destruction and marks its record as terminating.
2. Metal removes runtime, network, and disk resources through saved cleanup checkpoints.
3. Atlas removes its record after Metal confirms absence.

Public-address allocation has a separate retry path.

## Failure and recovery

Keep uncertain drafts and terminating records when Metal does not answer. [Find a problem](../operate/find-a-problem.md) starts with the assigned host, Metal progress, and Atlas jobs. For guest access, use the [console](console.md).

**Details:** [Atlas VM records](vm-records.md), [Metal reconciliation](reconciliation.md), [Atlas API](/api/atlas/), and [Metal API](/api/metal/).

::: details Source code and tests

- [Atlas VM service](../../atlas/vm/core/vm_service.py) owns draft creation and host calls.
- [Placement context](../../atlas/vm/core/placement/context.py) reserves host capacity.
- [Atlas reconciliation](../../atlas/vm/core/reconciliation.py) settles uncertain drafts and terminations.
- [Metal manager](../../metal/internal/vm/manager.go) stores the create request and checks retries.
- [Metal reconciliation](../../metal/internal/vm/reconcile.go) applies and removes host resources.
- [Atlas VM tests](../../atlas/vm/core/test_vm_service.py) and [Metal manager tests](../../metal/internal/vm/manager_test.go) check the two commit points.

:::
