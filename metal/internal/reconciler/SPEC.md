# reconciler: convergence loops

For Go code, follow the repository [Go anti-pattern rules](../../../llm/go-code-review-guide.md).

[Metal specification](../../SPEC.md) · overview: [Metal overview](../../../docs/develop/metal.md)

## Purpose

This package schedules convergence work. It owns no VM, image, snapshot, or traffic state.

## Types

| Type | Responsibility |
|---|---|
| `VirtualMachineReconciler` | Runs VM reconciliation passes. |
| `NewSleepTransferReconciler` | Runs sleep staging, compression, upload, and eviction in a separate two-worker pool. |
| `NewSleepRestoreReconciler` | Reconciles sleeping VMs and restores remote artifacts in a separate two-worker pool. |
| `ImageReconciler` | Caches images, prunes unused images, kernels, and staged snapshots. |
| `MigrationReconciler` | Advances each active destination migration. |
| `GuestChecker` | Pings running guests every minute, so a silent guest counts as unavailable. |
| `passScheduler` | Runs a pass at startup, on an interval, and on request. |

## Pass model

- Each operation has its own timeout.
- VM operations share a fixed number of slots across passes. A pass starts operations and does not wait for them, so one slow VM does not delay the next pass. A pass skips a VM whose operation still runs.
- Sleep uploads and restores each use two separate workers with the same timeout, backoff, wake, and shutdown behavior. They do not occupy normal VM reconciliation workers or wait for each other's pool.
- Errors are logged. A later pass retries safe operations.
- A failing VM waits twice as long after each failure, up to 1 minute. Wake clears the wait.
- Errors after cancellation are suppressed.

## Ownership

- metald owns each goroutine and its context.
- The VM manager owns every state decision and per-VM lock.

## Related

- [internal/vm/SPEC.md](../vm/SPEC.md) owns VM reconciliation.
- [internal/storage/SPEC.md](../storage/SPEC.md) owns image caching and pruning.
