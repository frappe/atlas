# VM migration code contract

The [migration guide](../../../../docs/compute/migration.md) explains the regional move. The [Metal migration engine](../../../../docs/compute/migration-engine.md) explains transfer, cutover, and recovery. This file defines the package boundary and invariants for code changes.

## Owners and records

| Owner | Code responsibility |
| --- | --- |
| `migration.Manager` | Source and destination records, per-VM workers, finish, and abort. |
| `vm.MigrationHost` | VM operations that migration can use. |
| `storage.MigrationTransfer` | Snapshot and dataset transfer. |

Each VM can have `machines/<vm-id>/migration/source.json` or `destination.json` beside its VM records. A source record blocks normal VM changes. A destination record reserves the VM ID, hides it from normal reads, and pauses reconciliation. A migration ID differs from the VM ID. The destination scans records to find the VM.

## Invariants

- One host allocation lock covers the destination capacity check and reservation. A retry with a different resize conflicts. A resize never shrinks the disk.
- A destination record read before a long operation must be read again before mutation. Otherwise an abort can be lost.
- Every destination record read-modify-write holds its VM migration lock. Code that already holds the lock uses `writeDestination`. Other code uses `mutateDestination`, which rereads under the lock. The lock is not reentrant.
- A transfer holds no VM lock while bytes move. The source retains the last acknowledged snapshot as the next incremental base. A received interval counts only when its GUID matches.
- Each external cutover step is checkpointed before the next step. The source removes its network before the final snapshot. An optional memory snapshot is bound to the final disk GUID and restored paused before `ready`.
- Snapshot status is derived from destination checkpoints. The optional API field adds no stored state. Disk-only progress omits it.
- `MemoryTransfer` uses `TransferProgress` for the memory snapshot and stays separate from disk intervals. Its sequence follows the final disk row, so retries keep one controller row. The API identifies it with optional `snapshot_type: "memory"`.
- Memory snapshot migrations activate only during `finishing`, after Atlas commits ownership. Activation retries inspect the existing process and never replay the original snapshot after guest execution.
- Finish and abort are mutually exclusive. The first request wins. Finish requires a destination in `ready`.
- A rollback failure keeps both records, locks, and the source disk. The source ends an active stream before unlock or destruction. Two hosts must never own one disk.
- A source lock expires only before the source stops. Its `expired` tombstone rejects the same migration ID. A `ready` destination does not expire.
- A terminal or failed destination reserves no capacity. A new migration replaces an aborted record only after its VM, lock, and dataset records are gone.
- A record decode error stops daemon startup.

## Extension points

`CreateDestination` persists the optional `has_memory_snapshot` request flag and rejects changes to it on an active retry. The `initialize_memory_migration_flags` startup patch handles existing records. [Record tests](records_test.go) check the upgrade path. `memory.go` owns memory snapshot transfer and its checkpoint. [Memory transfer tests](memory_test.go) check parallel completion, cancellation, source fencing, and checksum verification. `kernel.go` copies a missing kernel during copy. [Kernel tests](kernel_test.go) check the skip, the copy, and the 404 fallback. See the [migration engine guide](../../../../docs/compute/migration-engine.md) for upgrade defaults.

The daemon supplies concrete `vm.MigrationHost` and `storage.MigrationTransfer` values. Private interfaces are test seams within this package. The `vm` package does not import `migration`. The daemon injects `migration.Manager` as `vm.MigrationGuard` with `(*vm.Manager).SetMigrationGuard`.

When a transfer rule changes, check [destination tests](destination_test.go) and [storage transfer tests](../../storage/migration_transfer_test.go). The [Metal API specification](../../api/SPEC.md) owns route wiring.
