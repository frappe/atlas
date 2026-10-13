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

### Common CPU configuration

Set `firecracker.cpu_template_file` in `metald.toml` to a custom Firecracker CPU template JSON file. Metal reads the file at startup and applies it through `/cpu-config` before each cold boot. An absent setting uses Firecracker's default CPU configuration. Invalid JSON or an unreadable file stops daemon startup. Firecracker validates the template schema before guest boot.

Use Firecracker's [CPU template helper](https://github.com/firecracker-microvm/firecracker/blob/v1.16.1/docs/cpu_templates/cpu-template-helper.md) to compare guest CPUID and model-specific registers on every target CPU, kernel, and Firecracker combination. Verify the resulting template on each host, then test the guest and memory migration in both directions. Matching template files alone do not prove migration compatibility.

Changing the template requires a daemon restart and a cold boot of each VM that needs the new CPU configuration. Existing running and restored guests keep the CPU configuration in their memory state. Shared warm images and VM-local snapshots include the template digest in their compatibility marker.

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
