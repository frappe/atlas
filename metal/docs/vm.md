# VM functionality

[metal SPEC](../SPEC.md) · overview: [docs/architecture.md](architecture.md)

A virtual machine in metal is one firecracker guest with its own disk, network, and
optional memory snapshots. This is the broad view. The contract is in
[internal/vm/SPEC.md](../internal/vm/SPEC.md); the implementation is in
[internal/firecracker/SPEC.md](../internal/firecracker/SPEC.md).

## Lifecycle

```text
 create ----> created ----start----> running ----stop----> stopped ----start--> running
                                       |  ^
                                 pause |  | resume
                                       v  |
                                     paused
 destroy: any state -> destroyed        crash: running -> failed
```

`stopped` is a stop on request. `failed` is a guest that died on its own. Full
transition table: [internal/vm/SPEC.md](../internal/vm/SPEC.md).

## Operations

| Operation | Effect |
|---|---|
| Create | Allocate id, uid, network. Configure the guest. Boot on `Start`. |
| Start | Cold-boot, warm-load, or relaunch a stopped VM. |
| Stop | Graceful shutdown, or forced kill. Keeps disk and network. |
| Pause / Resume | Halt or run the vCPUs. Pause keeps memory. |
| Resize | Grow the disk. Grow-only. |
| Destroy | Free disk, network, socket, and files. |
| Snapshot / Restore / Promote | See [docs/snapshots.md](snapshots.md). |

Detail: [internal/firecracker/SPEC.md](../internal/firecracker/SPEC.md).

## Cold boot vs warm start

```text
COLD  boot the kernel and rootfs, then run
WARM  load a warm image's captured RAM and resume, so the guest skips boot
```

A warm image is made by promoting a memory snapshot. The warm load refreshes the
guest's ssh keys and clock through the metadata service before it runs. Detail:
[internal/firecracker/SPEC.md](../internal/firecracker/SPEC.md) and
[docs/snapshots.md](snapshots.md).

## Stop escalation

A graceful stop sends Ctrl+Alt+Del, waits 30 s, then escalates to a systemd stop
job. A forced stop sends SIGKILL. Detail:
[internal/firecracker/SPEC.md](../internal/firecracker/SPEC.md).
