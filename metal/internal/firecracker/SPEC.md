# firecracker: VM runtime code contract

For Go code, follow the [Go review guide](../../../llm/go-code-review-guide.md). The handbook owns [VM runtime behavior](../../../docs/compute/runtime.md), [disk encryption](../../../docs/compute/disk-encryption.md), and [console access](../../../docs/compute/console.md).

This package implements `vm.Runtime` and `vm.WarmRuntime`. It owns the jail, guest metadata, and Firecracker API calls inside one `metal-vm@` systemd unit. It holds no VM records. systemd owns the process lifecycle.

## Types and boundaries

| Type | Responsibility |
| --- | --- |
| `Runtime` | Runtime and warm-runtime interfaces. |
| `machine` | One VM operation, unit, and API client. |
| `Config` | Host directories and executable paths. |
| `firecracker/api.Client` | One VM Unix socket and serialized Firecracker requests. |
| `firecracker/api.Error` | Non-2xx status and fault message. |

`Start` chooses shared warm memory only for a fresh disk clone and exact shape. `ColdStart` never uses source guest memory. `Restore` and `RestorePaused` use VM-local saved state and return an error instead of silently cold-booting. See the [warm-memory incident](../../../docs/incidents/2026-09-24-guest-disk-corruption.md).

## Invariants

- Each launch discards the previous jail. The chroot base and kernel stay on the same file system because the kernel is hard linked. Jailer arguments in the systemd `EnvironmentFile` cannot contain spaces.
- The console master remains open while its VM runs. Startup adopts masters from the systemd descriptor store. See the [console package](../console/SPEC.md).
- Firecracker gets whole vCPUs rounded up from millicores. systemd applies the exact CPU quota. The API limit is 32000 millicores.
- The Firecracker boot source uses `initrd_path` when storage supplies an initrd. An encrypted boot requires that path.
- Image boot arguments cannot contain `atlas.disk_encryption`. Metal appends only `atlas.disk_encryption=luks2` when the VM specification selects LUKS2 encryption.
- An encrypted VM cannot use shared warm memory. It can use its own VM-local saved state for idle stop and restore.
- A memory snapshot restores only into the Firecracker build that wrote it. Binary size and modification time identify that build.
- `SaveAndStop` writes VM-local state to temporary files, publishes it with one atomic rename, then stops Firecracker. A running VM with an existing valid snapshot conflicts. A paused VM with one terminates Firecracker. A stopped VM with one is complete.
- Restore removes saved state only after the new jail loads its copy. Stop, restart, remove, and incompatible shape delete it.
- `RefreshDisk` changes live drive limits. `LimitDiskThroughput` sets a temporary migration limit. A nonpositive value cannot clear a limit.
- `Remove` deletes runtime files. The VM manager releases storage and network.

## Firecracker API client

The client sends JSON over a Unix socket. One lock per socket path covers the whole exchange because Firecracker rejects concurrent requests. Keep-alives are disabled because a new process can reuse the path. Non-2xx responses return `*Error`, even when the fault body cannot be decoded. Snapshot and drive paths are relative to the guest chroot.

Add one request type and method for a new Firecracker endpoint. A new guest operation also needs the matching `vm.Runtime` boundary and focused tests, such as [machine tests](machine_test.go) or [saved-state tests](saved_state_test.go).

## Related contracts

- [VM manager](../vm/SPEC.md) defines runtime calls.
- [Storage](../storage/SPEC.md) owns warm artifacts.
- [Platform](../platform/SPEC.md) owns systemd control.
