# vm: virtual machine contract

[internal SPEC](../SPEC.md) · overview: [docs/vm.md](../../docs/vm.md)

## Purpose

Package `vm` defines hypervisor-independent VM interfaces and value types. It owns no runtime state.

## Types

| Type | Role |
|---|---|
| `Driver` | Reserve, load, list, resize, and set desired VM state. |
| `VM` | Operate one VM and read its observed information. |
| `Spec` | CPU, memory, disk, image, network, SSH, hostname, and user data. |
| `ImageRef` | Immutable image identity, transport data, and local cache policy. |
| `Info` | Observed state, desired state, resource data, image data, and network data. |
| `State` | `unknown`, `created`, `running`, `paused`, `stopped`, `failed`, or `destroyed`. |
| `ExitStatus` | Process exit code and signal. |

## Driver boundary

```text
Create
Load
List
SetDesiredState
ReplaceSSHKeys
ResizeCompute
```

`Create` accepts a controller-supplied ID. Repeat create calls can refresh image transport and cache fields when reservation identity is unchanged.

## VM

```text
Start
Stop
Pause
Resume
Destroy
Wait
ResizeDisk
Info
```

Snapshot creation is a driver service in the Firecracker package. It is not part of the hypervisor-independent VM handle.

## State machine

The API stores a desired state. The reconciler compares it with the observed state and calls the required VM operation.

```text
desired running   -> Start or Resume
desired paused    -> Start when required, then Pause
desired stopped   -> Stop
desired destroyed -> Destroy and remove the reservation
```

A repeated request for the current desired state is valid.

## Snapshots and images

The VM package does not provide image list or delete operations. Images enter through VM reservations and controller sync policy.

`cache_image` retains compatible image artifacts. `memory_snapshot` requests an exact-shape host-local warm artifact when caching is enabled.

## scripts/

Host setup scripts live in `metal/scripts/`. They create required host services and ZFS parent datasets.

## Related

- [docs/vm.md](../../docs/vm.md) describes VM behavior.
- [internal/firecracker/SPEC.md](../firecracker/SPEC.md) implements these contracts.
- [internal/storage/SPEC.md](../storage/SPEC.md) and [internal/network/SPEC.md](../network/SPEC.md) describe host resources.
