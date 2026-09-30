# reconciler: convergence loops

For Go code, follow the repository [Go anti-pattern rules](../../../llm/go-code-review-guide.md).

[Metal specification](../../SPEC.md) · overview: [Metal overview](../../../docs/develop/metal.md)

## Purpose

This package schedules convergence work. It owns no VM, image, snapshot, or traffic state.

## Types

| Type | Responsibility |
|---|---|
| `VirtualMachineReconciler` | Runs VM reconciliation passes. |
| `ImageReconciler` | Caches images, prunes unused images and staged snapshots. |
| `MigrationReconciler` | Advances each active destination migration. |
| `passScheduler` | Runs a pass at startup, on an interval, and on request. |

## Pass model

- Each operation has its own timeout.
- The VM pass limits concurrency, so one slow VM cannot block others.
- Errors are logged. A later pass retries safe operations.
- Errors after cancellation are suppressed.

## Ownership

- metald owns each goroutine and its context.
- The VM manager owns every state decision and per-VM lock.

## Related

- [internal/vm/SPEC.md](../vm/SPEC.md) owns VM reconciliation.
- [internal/storage/SPEC.md](../storage/SPEC.md) owns image caching and pruning.
