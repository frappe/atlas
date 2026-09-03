# firecracker: the microVM driver

[internal SPEC](../SPEC.md) · overview: [docs/architecture.md](../../docs/architecture.md)

## Purpose

Package `firecracker` implements the `vm` contract on Firecracker. The premise is
that metald is only a client: each virtual machine is a jailer'd firecracker process
that runs as the systemd template unit `metal-vm@<id>.service`. metald holds no child
process. It drives a VM through systemd over D-Bus and through the VM's own API
socket. All per-VM truth lives on disk, so metald is stateless and survives a
restart.

## Types

| Type | Role |
|---|---|
| `Driver` | Implements `vm.VMDriver`. Owns the collaborators and guards id allocation with a mutex. |
| `machine` | Implements `vm.VM`. A client-side handle: a back-pointer to the `Driver`, the VM's `vmConfig`, and an API client. Holds no process. |
| `Config` | Host paths and the id range: `MachinesDir`, `SocketsDir`, `JailerBin`, `FirecrackerBin`, `IDs`. All layout helpers hang off it. |
| `vmConfig` | Per-VM state persisted as `machines/<id>/config.json`: `ID`, `UID`, `GID`, `IP`, `MAC`, `Sock`, `Spec`. |

## Composition

The `Driver` composes the three host packages plus a per-VM API client.

```text
Driver
 ├─ systemd.Manager    start/stop/limits/state of the unit
 ├─ storage.Resolver   kernel, rootfs, disk snapshots
 ├─ network.Allocator  netns and tap0
 └─ api.Client         firecracker REST over each VM's socket
```

## Cold boot

```text
Create(spec):
  id = UUIDv7 ; allocate uid=gid under mu ; write config.json
  net.Allocate -> netns metal-<id>, tap0, MAC, IP
  warm image?  write the warmload mark, return (the load waits for Start)
  else bootPrep:
     write jailer.env (JAILER_ARGS) ; link the short socket symlink
     units.Start metal-vm@<id>  ->  jailer  ->  firecracker (in the chroot)
     units.SetLimits (memory, cpu)
     waitSocket
     images.Prepare -> kernel + rootfs block node in the chroot -> BootConfig
     configure(api): machine-config, boot-source, drive(s), network, [MMDS]
  => Created (guest not booted)

Start:  api.InstanceStart  =>  Running
```

## Cold start vs warm start

```text
COLD  Create -> bootPrep -> configure -> Start(InstanceStart)     boots kernel + rootfs
WARM  Create -> write warmload mark -> first Start -> warmLaunch -> loadLaunch:
         fresh unit, PrepareRootfs, stage state + mem into the chroot,
         api.LoadSnapshot(resume_vm=false) -> [PutMmds refresh] -> api.Resume
      resumes from captured RAM
```

`loadLaunch` also serves restore of a memory snapshot. `LoadSnapshot` runs paused so
an MMDS refresh lands before the guest runs.

## Stop escalation

```text
Stop(force=true):   units.Kill SIGKILL
Stop(force=false):  api.SendCtrlAltDel
                    wait up to 30 s (units.Wait)
                       guest exits        -> done
                       timeout, no i8042  -> units.Stop (systemd stop job)
always: units.Wait, then units.ResetFailed
        so a deliberate stop reports stopped, and only a crash reports failed
```

## Snapshot, restore, promote

```text
Snapshot(memory):
  Running --api.Pause--> Paused
  images.Snapshot            disk snapshot, quiescent
  api.CreateSnapshot Full    state + mem into a uid-owned chroot dir
  move files -> machines/<id>/snapshots/<name>/{state,mem}   outlive the chroot
  defer api.Resume           always, even on error

Restore:  stop -> images.Restore (rollback) -> warm? loadLaunch : stay stopped
Promote:  needs a memory snapshot -> images.Promote (zfs send | recv, full copy)
```

## State derivation

```text
units.Status.ActiveState:
  "failed"                    -> Failed
  "inactive" / "deactivating" -> Stopped
  active -> api.InstanceInfo.State:
       "Not started" -> Created
       "Paused"      -> Paused
       else          -> Running
```

## Statelessness and the socket

```text
per-VM truth on disk:  machines/<id>/config.json, snapshots/, the warmload mark,
                       plus the systemd unit state
metald restart:        Load / List rebuild handles from config.json; no in-memory registry
socket:                metald dials SocketsDir/<id>.sock (short symlink)
                       -> chroot/run/firecracker.socket
                       the jail path repeats the id and overflows the 108-byte
                       sockaddr limit, so the short symlink is dialed instead
```

## Related

- [internal/firecracker/api/SPEC.md](api/SPEC.md) the REST client over the VM socket.
- [internal/vm/SPEC.md](../vm/SPEC.md) the contract this package implements.
- [internal/systemd/SPEC.md](../systemd/SPEC.md), [internal/storage/SPEC.md](../storage/SPEC.md), [internal/network/SPEC.md](../network/SPEC.md) the collaborators.
- [docs/vm.md](../../docs/vm.md), [docs/snapshots.md](../../docs/snapshots.md) the broad overviews.
- [docs/host-layout.md](../../docs/host-layout.md) the on-disk layout and the socket symlink.
