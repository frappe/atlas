# How to think through a change

Use for any non-trivial fix, feature or refactor, and for discovery. Show the reasoning in chat (proposal and review), never in code or docs. A short cause can go in the commit body.

## Method

Answer each in a few lines before changing code:

1. **Problem and evidence:** what is wrong, for whom, how you know (error, log, request, measurement). Say if you cannot reproduce it.
2. **Trace the path:** entry point to every read and write. Name each state owner (doctype field, Metal record, BPF map, file) and every caller.
3. **Invariants:** what must stay true (one writer per field, one VM per IP, old clients work, draft before Metal call).
4. **Two candidate fixes:** lines, owners, contracts, concurrency and failure behavior, fit with existing patterns. Pick one; say why the other loses.
5. **Failure and rollback:** crash halfway, runs twice, old data, how to undo.
6. **Falsifiable verification:** a test, benchmark or live check that would fail if you are wrong. Run it.

Present 1, 3, 4 and 5 compactly; wait for approval if structural.

## Refactor method

1. **Pin behavior:** what callers rely on (returns, errors, side effects, timing, API shapes) and how you will check it.
2. **Find all callers:** scripts, jobs, hooks, Desk JS, generated clients, other services.
3. **Choose the owner:** where the state lives, not where it is called.
4. **Keep contracts:** APIs, stored data, eBPF maps and wire formats stay compatible; internal names may change.
5. **Small steps:** rename, move, then simplify; each step builds and passes.
6. **Remove the old way** everywhere, or propose that separately.
7. **Verify** with the step 1 checks.

## Example: VM placement (September 2026)

- **Problem:** `PlacementService.select_server` took the whole create request, looked up strategies in a dict, read Atlas Settings as a full document; strategies got the VM object; a "capacity pending" state appeared and capacity expansion ran inside the request. 100 concurrent creates showed lock waits and slow rejections.
- **Path:** create API → `VirtualMachine.create` → placement → host row lock → reservation → commit → Metal `PUT`. Metal Server rows own capacity; the VM row owns the reservation; Atlas Settings owns the strategy. Callers: create, automatic migration, simulator.
- **Invariants:** no double reservation; answer or reject within budget; no network call under a host lock; create API unchanged.
- **Option A (rejected):** widen the deadline and add a server-side queue. Every create pays tail latency for a burst problem; the queue adds state and failure modes.
- **Option B (chosen):** strategies use `@register`, one abstract base and one method taking `PlacementRequirements` (CPU, memory, disk, architecture) and returning a host. No VM object in placement. Lock one row with `get_value(..., for_update=True)`, read settings with `get_cached_value`, short negative cache for a full pool, return "out of capacity, retry later". Capacity expansion moves to a background job. Tenant ID stayed out of requirements: spreading was a preference, not a limit.
- **Failure:** a crash before commit releases the lock; retries start fresh; rollback is a plain revert with no data shape change.
- **Verification:** a two-session test proved the row locks only when the capacity predicate holds; 100 and 200 hosts with 2,000 placements gave p99 under 100 ms healthy and fast rejection when full; the simulator ran every strategy.
- **Lesson:** find the state owner, name the invariant, reject the easy knob that moves cost to every request.
