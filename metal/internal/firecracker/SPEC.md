# firecracker: VM runtime code contract

For Go code, follow the [Go review guide](../../../llm/go-code-review-guide.md). The handbook owns [VM runtime behavior](../../../docs/compute/runtime.md) and [console access](../../../docs/compute/console.md).

This package implements `vm.Runtime` and `vm.WarmRuntime`. It owns the jail, guest metadata, and Firecracker API calls inside one `metal-vm@` systemd unit. It holds no VM records. systemd owns the process lifecycle.

## Types and boundaries

| Type | Responsibility |
| --- | --- |
| `Runtime` | Runtime and warm-runtime interfaces. |
| `machine` | One VM operation, unit, and API client. |
| `Config` | Host directories and executable paths. |
| `Config.CPUTemplate` | Immutable custom CPU template applied before cold boot. |
| `firecracker/api.Client` | One VM Unix socket and serialized Firecracker requests. |
| `firecracker/api.Error` | Non-2xx status and fault message. |
| `sshSession` | Owns an SSH unit, console PTY, MMDS key, and session files. |

`Start` chooses shared warm memory only for a fresh disk clone and exact shape. `ColdStart` never uses source guest memory. `Restore` and `RestorePaused` use VM-local saved state and return an error instead of silently cold-booting. See the [warm-memory incident](../../../docs/incidents/2026-09-24-guest-disk-corruption.md).

`RestoreMigrationSnapshot` validates the bundle identity and VM shape and loads it paused with the destination's user ID. Firecracker and KVM decide host compatibility during snapshot load. Source software and CPU metadata are recorded but do not block migration. See [migration](../../../docs/compute/migration-engine.md) for the transfer and activation contract.

## Invariants

- Each launch discards the previous jail. The chroot base and kernel stay on the same file system because the kernel is hard linked. Jailer arguments in the systemd `EnvironmentFile` cannot contain spaces.
- The console master remains open while its VM runs. Startup adopts masters from the systemd descriptor store. See the [console package](../console/SPEC.md).
- Firecracker gets whole vCPUs rounded up from millicores. systemd applies the exact CPU quota. The API limit is 32000 millicores.
- A VM-local memory snapshot requires a matching Firecracker build. Binary size and modification time identify that build. Migration delegates build compatibility to Firecracker.
- Shared warm images and VM-local snapshot matching include the configured CPU template digest.
- `SaveAndStop` writes VM-local state to temporary files, publishes it with one atomic rename, then stops Firecracker. A running VM with an existing valid snapshot conflicts. A paused VM with one terminates Firecracker. A stopped VM with one is complete.
- VM-local snapshots record the source TAP gateway MAC. Local and migration restores apply it before snapshot load so restored ARP and NDP caches remain valid. Migration requires this field; local snapshots without it keep the existing TAP MAC.
- Restore removes saved state only after the new jail loads its copy. Stop, restart, remove, and incompatible shape delete it.
- `RefreshDisk` changes live drive limits. Unlimited values send zero-sized buckets to clear temporary migration limits.
- `Remove` deletes runtime files. The VM manager releases storage and network.

## SSH console sessions

- A session ID is `ssh-` plus 16 hex digits. It names the console and descriptor, and follows `metal-` in the unit name.
- `RecoverSSHSessions` keeps adopted sessions and ends sessions without a console or record. See the [console guide](../../../docs/compute/console.md) for reconnect timing.
- `end` stops the unit, closes the console, removes the MMDS key, and deletes session files. Each step tolerates missing parts.
- A failed `sshSessionUnits.Wait` does not end a session, because metald shutdown closes the D-Bus connection.

## Firecracker API client

The client sends JSON over a Unix socket. One lock per socket path covers the whole exchange because Firecracker rejects concurrent requests. Keep-alives are disabled because a new process can reuse the path. Non-2xx responses return `*Error`, even when the fault body cannot be decoded. Snapshot and drive paths are relative to the guest chroot.

Add one request type and method for a new Firecracker endpoint. A new guest operation also needs the matching `vm.Runtime` boundary and focused tests, such as [machine tests](machine_test.go) or [saved-state tests](saved_state_test.go).

## Related contracts

- [VM manager](../vm/SPEC.md) defines runtime calls.
- [Storage](../storage/SPEC.md) owns warm artifacts.
- [Platform](../platform/SPEC.md) owns systemd control.
