# How Metal runs a VM

Metal prepares the guest and controls it through Firecracker's local API. systemd owns the VM process.

## Start a guest

1. Prepare the disk and network.
2. Create the jail and serial console.
3. Start the `metal-vm@` systemd unit.
4. Wait for Firecracker's API socket, configure the machine, and start the guest.

Metal inspects systemd first: an inactive unit has no live Firecracker API.

### Cold and warm starts

**Shared warm memory works only for a first boot from a fresh disk clone, with the exact VM shape.** Later starts cold-boot the existing disk.

This rule prevents the [warm-memory disk corruption incident](../incidents/2026-09-24-guest-disk-corruption.md).

### Resource limits

Firecracker receives guest CPUs rounded up from the millicore entitlement. systemd applies the exact CPU quota. Firecracker drive limiters enforce disk throughput and IOPS.

## Stop, pause, and restore a guest

Power requests accept `running`, `paused`, or `stopped`.

| Action | Saved state | Result |
| --- | --- | --- |
| Explicit stop | Removed. | Guest shutdown requested. Metal waits for the unit. |
| Automatic idle stop | Memory and device state saved under `machines/<id>/saved-state/`. | Desired state stays `running`. Observed state becomes `stopped`. |
| Guest-directed IPv4 or IPv6 traffic | Same VM state restored. | Idle VM wakes. |
| Restart or shape change | Removed. | Next start follows the normal boot path. |
| VM removal | Removed. | Guest resources are deleted. |

[Sleepy VMs](sleepy-vms.md) explains automatic idle stop and wake-up.

## Limits and recovery

### Daemon restart

Running guests survive only when systemd retains their console master descriptors and Metal adopts them. The installed unit uses systemd's file-descriptor store.

Without that store, daemon exit closes the consoles and stops the guests. Startup logs report how many running consoles Metal adopted.

### Failed restore

Metal keeps valid VM-local saved state for another try. It does not substitute a cold boot for a requested restore or reuse shared warm memory with an existing disk.

::: details Source code and tests

- [Firecracker runtime](../../metal/internal/firecracker/runtime.go) owns guest launch and inspection.
- [Machine operations](../../metal/internal/firecracker/machine.go) distinguish first boot, cold boot, stop, and saved-state restore.
- [Console broker](../../metal/internal/console/serial_broker.go) manages serial PTYs.
- [Console adoption](../../metal/cmd/metald/main.go) checks the systemd descriptor store at startup.
- [Idle watcher](../../metal/internal/vm/idle_watcher.go) decides when to save or restore a guest.
- [Runtime tests](../../metal/internal/firecracker/machine_test.go) check start and stop behavior.

:::

## Forced-stop errors

If systemd reports a signal error during a forced stop, Metal requests a unit stop and waits for that job to complete. It clears the failed unit state only after termination completes. If the stop job also fails, Metal returns both errors and preserves any saved memory for recovery.
