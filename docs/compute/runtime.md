# How Metal runs a VM

Metal prepares the guest and controls it through Firecracker's local API. systemd owns the VM process.

## Start a guest

1. Prepare the disk and network.
2. Create the jail and serial console.
3. Start the `metal-vm@` systemd unit.
4. Wait for Firecracker's API socket, configure the machine, and start the guest.

Metal inspects systemd first: an inactive unit has no live Firecracker API.

### Cold and warm starts

**Shared warm memory works only for a first boot from a fresh disk clone, with the exact vCPU count and memory and a disk at least as large as the warm disk.** Later starts cold-boot the existing disk.

This rule prevents the [warm-memory disk corruption incident](../incidents/2026-09-24-guest-disk-corruption.md).

A warm start grows the clone to the VM disk size before the snapshot loads. Before resume, Metal refreshes the drive, so the guest sees the larger disk and gets its disk limits. The guest image grows its file system when the disk grows.

### Resource limits

Firecracker receives guest CPUs rounded up from the millicore entitlement. systemd applies the exact CPU quota. Firecracker drive limiters enforce disk throughput and IOPS.

### Common CPU configuration

Metal selects a built-in Intel x86-64, AMD x86-64, or ARM64 CPU template at startup and applies it before every cold boot. `firecracker.cpu_template_file` overrides this selection with an explicit JSON file. See [guest CPU templates](cpu-templates.md) for the profiles, migration limits, validation, and snapshot upgrade requirements.

## Stop, pause, and restore a guest

Power requests accept `running`, `paused`, or `stopped`.

| Action | Saved state | Result |
| --- | --- | --- |
| Explicit stop | Removed. | Guest shutdown requested. Metal waits for the unit. |
| Automatic idle stop | Memory and device state saved under `machines/<id>/saved-state/`. | Desired state stays `running`. Observed state becomes `stopped`. |
| Guest-directed IPv4 or IPv6 traffic, except ICMP | Same VM state restored. | Idle VM wakes. |
| Restart or shape change | Removed. | Next start follows the normal boot path. |
| VM removal | Removed. | Guest resources are deleted. |

[Sleepy VMs](sleepy-vms.md) explains automatic idle stop and wake-up.

### Guest clock after a memory restore

A guest restored from memory keeps the wall clock of its snapshot. A warm image can be days old, and time sync needs network and a server reply before it corrects the clock. Until then, the guest rejects tokens that other services issued at the current time, and it expires its sessions and cache entries when the clock jumps.

Before every resume from memory, Metal writes the host wall clock to the guest metadata as `meta-data/host-time-unix-ms`. This applies to warm starts, saved-state wakes, and memory snapshot migrations. The guest `atlas-metadata.service` steps the clock forward to that time when the guest clock is more than 2 seconds behind it. It never steps the clock back. Time sync then corrects the remaining gap.

Images built before this change ignore the key and wait for time sync.

## Limits and recovery

### Daemon restart

Running guests survive only when systemd retains their console master descriptors and Metal adopts them. The installed unit uses systemd's file-descriptor store.

Without that store, daemon exit closes the consoles and stops the guests. Startup logs report how many running consoles Metal adopted.

### Failed restore

The runtime returns restore errors to the VM manager. The manager owns [wake recovery](sleepy-vms.md#wake-an-offloaded-vm), including cold-boot fallback. Metal never reuses shared warm memory with an existing disk.

::: details Source code and tests

- [Firecracker runtime](../../metal/internal/firecracker/runtime.go) owns guest launch and inspection.
- [Machine operations](../../metal/internal/firecracker/machine.go) distinguish first boot, cold boot, stop, and saved-state restore.
- [Memory snapshot launch](../../metal/internal/firecracker/launch.go) writes the restore metadata, including the host time.
- [Guest metadata agent](../../atlas/vm/scripts/guest/apply-metadata) steps the guest clock forward.
- [Console broker](../../metal/internal/console/serial_broker.go) manages serial PTYs.
- [Console adoption](../../metal/cmd/metald/main.go) checks the systemd descriptor store at startup.
- [Idle watcher](../../metal/internal/vm/idle_watcher.go) decides when to save or restore a guest.
- [Runtime tests](../../metal/internal/firecracker/machine_test.go) check start and stop behavior.

:::
