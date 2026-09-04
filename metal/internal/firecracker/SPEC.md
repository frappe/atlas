# firecracker: VM driver

[internal SPEC](../SPEC.md) · overview: [docs/architecture.md](../../docs/architecture.md)

## Purpose

Package `firecracker` implements the `vm` contracts. Systemd owns each jailed Firecracker process.

## Types

| Type | Role |
|---|---|
| `Driver` | VM reservations, desired state, host dependencies, locks, and warm artifact creation. |
| `machine` | One VM handle with persisted configuration and a Firecracker API client. |
| `Config` | Machine paths, socket paths, binaries, and user ID range. |
| `vmConfig` | Reservation, desired state, resource ownership, and cleanup progress. |
| `vmStatus` | Observed state, error, and update time. |

The driver receives separate VM storage, image, snapshot, systemd, and network dependencies.

## Composition

```text
Driver
 ├─ systemd manager
 ├─ VM storage
 ├─ image store
 ├─ snapshot store
 ├─ network allocator
 └─ Firecracker API client for each machine
```

## Reservation and reconciliation

```text
Create(id, specification)
   -> validate the supplied ID and reservation
   -> allocate a host user ID
   -> derive network values
   -> write config.json with desired running
   -> return without starting Firecracker

reconciler
   -> compare desired and observed states
   -> call Start, Stop, Pause, Resume, or Destroy
   -> write status.json
```

Repeat create calls can refresh signed image URLs and cache policy. They cannot change reservation identity.

## Cold boot

```text
allocate the Linux network
write jailer.env and socket link
start the systemd unit
set resource limits
wait for the Firecracker socket
prepare the kernel and VM disk
configure machine, boot source, drive, network, and MMDS
start the instance
```

## Cold start vs warm start

### Warm start

The driver selects warm artifacts by image identity, VM shape, and Firecracker compatibility. It loads the snapshot while paused, updates MMDS, and resumes the VM.

If warm start fails, the caller removes the attempted VM disk and starts cold.

## Warm artifact creation

The image reconciler calls `EnsureMemorySnapshot`. The driver creates a temporary VM without egress, waits five minutes, and pauses it.

The driver captures Firecracker state and memory. The image store copies these files and the disk snapshot into the warm cache.

## Stop escalation

Stop sends Ctrl+Alt+Del and waits 30 seconds. It sends `SIGKILL` when the guest does not stop.

### Destroy

Destroy records progress for systemd, network, and storage cleanup. It removes the VM directory only after all cleanup steps succeed.

## Snapshot, restore, promote

### Machine image snapshot

`CreateSnapshot` generates a UUIDv7 staging ID. It briefly pauses a running VM and asks the snapshot store to stage the disk and kernel.

This operation does not capture memory. It does not support in-place restore or promotion.

### Internal warm snapshot load

A new VM can load a compatible local warm snapshot. If the load fails, the driver removes the attempted disk and uses a cold boot.

### Policy-driven warm artifact

`EnsureMemorySnapshot` creates a local warm artifact for an exact image and VM shape. It does not create a public image reference.

## State derivation

The driver combines systemd state, Firecracker state, desired state, and the last reconciliation error. Unknown runtime values produce `StateUnknown`.

## Statelessness and the socket

`config.json` stores reservation and cleanup state. `status.json` stores observed state. A short socket link avoids the Unix socket path limit.

## Related

- [internal/firecracker/api/SPEC.md](api/SPEC.md) describes the Firecracker client.
- [internal/vm/SPEC.md](../vm/SPEC.md) defines the implemented contracts.
- [internal/storage/SPEC.md](../storage/SPEC.md) and [internal/network/SPEC.md](../network/SPEC.md) describe host resources.
- [docs/vm.md](../../docs/vm.md) gives the broad VM lifecycle.
