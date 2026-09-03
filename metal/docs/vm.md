# VM functionality

[metal SPEC](../SPEC.md) · contract: [internal/vm/SPEC.md](../internal/vm/SPEC.md)

A virtual machine has persisted metadata, desired state, and observed host state. The controller requests state changes, and Metal reconciles them.

## Lifecycle

```text
reservation -> unknown -> running <-> paused
                           |
                           v
                         stopped

any retained state -> desired destroyed -> cleanup -> removed
runtime error -> failed
```

Create reserves the supplied VM ID and sets the desired state to `running`. It does not wait for Firecracker to start.

Start, stop, pause, resume, and terminate return `202`. Poll the VM until `state` matches `desired_state`.

## Operations

| Operation | Effect |
|---|---|
| Create | Store a VM reservation and request `running`. |
| Start | Request `running`. |
| Stop | Request `stopped`. Keep the VM disk and network reservation. |
| Pause | Request `paused`. |
| Resume | Request `running`. |
| Terminate | Request cleanup of all VM resources. |
| Compute resize | Change CPU and memory while stopped, then request `running`. |
| Disk resize | Grow the VM disk. |
| SSH key replacement | Replace all keys and refresh MMDS when active. |
| Snapshot | Create rootfs and kernel image staging. |

Metal does not support in-place snapshot restore or promotion.

## Cold boot vs warm start

Cold boot clones an image disk, links the kernel, configures Firecracker, and starts the guest.

Warm boot loads host-local disk, state, and memory artifacts for an exact image and VM shape. If warm boot fails, Metal uses cold boot.

## Stop escalation

Metal sends Ctrl+Alt+Del and waits up to 30 seconds. It sends `SIGKILL` when the guest does not stop.

## Cleanup

Terminate stores the desired `destroyed` state. Reconciliation records cleanup progress for systemd, network, and storage.

Metal removes the VM directory only after all cleanup steps succeed.

## Design notes

- The controller supplies the VM ID, so reservation retries use one stable resource.
- Desired state keeps API requests fast and lets reconciliation retry host operations.
- Observed state remains separate because process changes are asynchronous.
- Stop keeps the disk for restart. Terminate removes all owned resources.
- Compute and disk resize use separate endpoints because their safety rules differ.
- Warm boot is an optimization. Cold boot remains the reliable path.
