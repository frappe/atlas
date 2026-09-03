# vm: hypervisor-agnostic VM contract

[internal SPEC](../SPEC.md) · overview: [docs/vm.md](../../docs/vm.md)

## Purpose

Package `vm` defines the Virtual Machine contract that metald uses. It holds interfaces and
value types only. It names no hypervisor. `internal/firecracker` implements it.

## Types

The package owns no runtime state. It is the contract every other package points
at, so it imports nothing internal.

| Type | Kind | Role |
|---|---|---|
| `VMDriver` | interface | Factory over VMs: `Create`, `Load`, `List`, `Images`, `DeleteImage`, `Type`. |
| `VM` | interface | Per-VM handle: lifecycle, snapshot, resize, `Info`. Holds no process. |
| `Spec` | struct | Create request: `VCPUs`, `MemMiB`, `DiskMiB`, `Image`, `Network`, `SSHKeys`. |
| `Info` | struct | Read model returned by `Info()`. Includes disk and snapshot counts. |
| `State` | string enum | `created`, `running`, `paused`, `stopped`, `failed`, `destroyed`. |
| `Snapshot` | struct | One snapshot. `Memory` marks a paired RAM and device capture. |
| `Image` | struct | A template VMs clone from. `Warm` marks a captured memory image. |
| `ExitStatus` | struct | `Code` and `Signal` from `Wait`. |
| `ErrNotFound`, `ErrConflict` | error | Unknown id, and invalid-for-state. Drivers return these. |

`SSHKeys` are public keys served to the guest through MMDS at boot. See
[docs/networking.md](../../docs/networking.md).

## Driver boundary

`VMDriver` makes and finds VMs. Each call returns a `VM` handle. The handle carries
no child process. It reaches its resources through the driver that made it.

```text
   VMDriver  (factory)
   Create · Load · List · Images · DeleteImage · Type
        |
        |  returns
        v
     VM  (per-VM handle)
   Start · Stop · Pause · Resume · Destroy · Wait
   Resize · Snapshot · Snapshots · DeleteSnapshot
   RestoreSnapshot · Promote · Info

   implemented by internal/firecracker
   Driver -> VMDriver,  machine -> VM
```

## State machine

Happy path:

```text
  Create        Start          Stop           Start
    |             |              |              |
    v             v              v              v
 created ----> running ----> stopped ----> running ...
                 |  ^
           Pause |  | Resume
                 v  |
               paused
```

Full transitions:

| From | Event | To | Note |
|---|---|---|---|
| (none) | `Create` | created | Warm image stays created. Memory load waits for first `Start`. |
| created | `Start` | running | Cold boot, or warm load for a warm image. |
| stopped | `Start` | running | Relaunch: fresh jailer process, reuse the disk and netns. |
| failed | `Start` | running | Relaunch. |
| running | `Start` | (same) | `ErrConflict`. |
| running | `Pause` | paused | Halts vCPUs. Keeps the process and memory. |
| paused | `Resume` | running | |
| running, paused | `Stop` | stopped | Graceful Ctrl+Alt+Del, else `SIGKILL`. Keeps disk and netns. |
| running | crash or kill | failed | The guest died on its own. |
| any live state | `Destroy` | destroyed | Terminal. Frees disk, netns, socket, and files. |
| running | `Snapshot` (memory) | running | Pause, capture, resume. The pause is brief. |
| any | `RestoreSnapshot` (memory) | running | Resumes at the captured instant. |
| any | `RestoreSnapshot` (disk) | stopped | Cold-boots from the rolled-back disk on next `Start`. |

`stopped` means a stop on request. `failed` means the guest died on its own.

## Snapshots and images

A snapshot always captures the disk. When `memory` is true it also captures RAM
and device state, paired with the disk snapshot, so a restore is consistent.
`Promote` turns a memory snapshot into a standalone warm `Image`. Full detail:
[internal/storage/SPEC.md](../storage/SPEC.md) and [docs/snapshots.md](../../docs/snapshots.md).

## scripts/

One-time host setup. An operator runs each script as root. metald does not run
them at runtime.

| Script | Role |
|---|---|
| `scripts/zfs-setup.sh` | Makes the file-backed zpool `metal` with `images/` and `vms/` parents. |
| `scripts/net-setup.sh` | Enables `ip_forward` and NATs transit `10.0.0.0/8` out the uplink. |

## Related

- [docs/vm.md](../../docs/vm.md) broad VM overview.
- [internal/firecracker/SPEC.md](../firecracker/SPEC.md) the implementation.
- [internal/storage/SPEC.md](../storage/SPEC.md), [internal/network/SPEC.md](../network/SPEC.md), [internal/systemd/SPEC.md](../systemd/SPEC.md) the substrate the driver composes.
