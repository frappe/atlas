# Reliability and operations

Code must work at 3 a.m. without a babysitter and give users clear, fast feedback. Reliability comes from good defaults and clear ownership, not extra layers.

## Pre-mortem

Answer in the design, a few lines each. "Cannot happen" needs a one-line reason.

1. Runs twice? Stops halfway? Killed between steps?
2. Remote side (Metal, provider, proxy, Central, object storage) slow, down, or errors after doing the work?
3. 200 hosts, hundreds of VMs per host, 100 concurrent requests?
4. Who else writes this data: request, job, Desk user, reconciler?
5. What does the user see while running, on failure, on success?
6. How does an operator learn it broke and fix it without a database console?
7. Which existing client, host or record could break?
8. How do we roll out and undo?

## Multi-step operations

For operations with several steps or a remote side effect (provisioning, migration, attach, upload). A local write needs no extra state.

- Explicit states with one owner (desired and observed, or a small fixed status set).
- Every step idempotent; a rerun after success or crash is safe.
- Persist intent (draft, provider ID, job) before the side effect.
- A reconciler or scheduled job finishes or cleans non-terminal states. Nothing relies on one request or enqueue.
- Terminal states stay terminal and record `failure_message` and how to retry.

## Timeouts and backpressure

- Every network call, subprocess and lock wait has a timeout.
- Bounded retries of idempotent operations on transient errors only (connection refused, 502, 503, 504, timeouts). Fail fast otherwise and show the body. Short waits over long backoff.
- Cap batches, queues, lists, uploads and fan-out. Return "out of capacity, retry later" instead of queueing forever.
- Latency-critical paths have a budget (placement).

## Data integrity

- Enforce invariants in the database: unique indexes, required fields, `set_only_once`, immutable fields (region ID).
- Validate at the boundary with typed models.
- No stored value that can drift from its source.
- Migration patches safe to run twice and on large tables.
- Deletes explicit and scoped by stored IDs. Archive first, purge later.
- Snapshot before destructive maintenance.

## Resource lifecycle

- Everything created has cleanup: cloud resources, datasets, files, temporary keys, sessions, goroutines, processes, firewall rules, DNS records.
- Scheduled sweepers for leaks (orphan files, stale drafts, expired grants, timed-out tasks).
- Startup recovers crash leftovers. A daemon restart keeps VMs and consoles alive.
- In-place upgrades without dropped traffic: graceful reload, socket handoff, file descriptor store.

## Observability

- Log at the boundary with operation, resource ID, tenant, host and an ID that also appears in the user's error.
- Never swallow upstream errors; map to a clear error and log the original.
- State readable without SQL: status, last error, last sync, progress, task output.
- Health checks test the real dependency, not only the process.
- Make staleness visible ("binary left at version X", "last synced 10 minutes ago").

## End-user experience

- Fast first response; long work async with `202` and a pollable state and progress.
- Understandable states; no internal or misleading phases.
- Errors say what happened and what to do, with a stable code. No stack traces.
- Safe defaults without configuration. Destructive actions confirmed, under Dangerous Actions.
- One action does one thing; return an error rather than change another setting.
- Old clients keep working ([design.md](design.md#api-design)).

## Testing

- Test failure paths: partial failure, retry after crash, lost response, concurrency, empty pool, full host.
- Test invariants and contracts (API shape, compatibility, idempotency), not constants.
- Risky infrastructure: on a development host, kill the daemon mid-operation, restart, confirm convergence.
- Benchmark at fleet size before claiming performance.

## Operations

- Each job, daemon or schedule has an owner, timeout, dedupe key, failure log and visible running state.
- Each setting has a safe default and a reason. Remove settings nobody changes.
- Runbooks for manual work (upgrade, recover, rotate) in the handbook, current state only.
- Rollback plan: commit to revert, data to restore, hosts to reinstall.

## Definition of done

Pre-mortem answered; idempotent, bounded, observable, recoverable; concurrency safe ([design.md](design.md#concurrency-and-performance)); old clients and data work; clear user states and errors; docs and runbook match; an operator can fix a failure without reading code.
