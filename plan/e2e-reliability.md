# E2E reliability fixes

Four targeted fixes for the slow / flaky e2e runs. Each is independently
landable. Sequence is `(3) → (1) → (6) → (2)` because (3) unbreaks phase 4
on its own, (1) cuts iteration time for everything that follows, and (2) is
defensive cleanup that has no value until (1) is in.

The goal is to take a phase-4-through-6 dev cycle from ~20 min (with one
phase 4 timeout) to ~3 min on the warm path.

## Fix 3 — Repair phase 4 test data

### Why first

Phase 4 cannot pass today regardless of infrastructure. The current image
fixture in [`../atlas/tests/e2e/phase_4.py`](../atlas/tests/e2e/phase_4.py)
has placeholder `0000…` SHA256s and points at `firecracker-ci/v1.10`, which
no longer hosts `ubuntu-24.04.squashfs`. The `sync-image.sh` script runs
`sha256sum -c` against the placeholders and fails.

### Change

In [`../atlas/tests/e2e/phase_4.py`](../atlas/tests/e2e/phase_4.py),
replace the `DEFAULT_IMAGE` literal:

```python
DEFAULT_IMAGE = {
    "image_name": "ubuntu-24.04",
    "description": "Firecracker CI Ubuntu 24.04 rootfs",
    "kernel_url": "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.12/x86_64/vmlinux-6.1.128",
    "kernel_filename": "vmlinux-6.1.128",
    "kernel_sha256": "27a8310b9a727517e9eb02044524b6ceb77de5728e3491b6974d5c846227ecc8",
    "rootfs_url": "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.12/x86_64/ubuntu-24.04.squashfs",
    "rootfs_filename": "ubuntu-24.04.ext4",
    "rootfs_sha256": "88821a26b5a38c92b84a064d452167d7f80f9e17cf4441d1ebbae7569e340aee",
    "default_disk_gigabytes": 4,
}
```

Move the literal into [`../atlas/tests/e2e/_shared.py`](../atlas/tests/e2e/_shared.py)
as `DEFAULT_IMAGE` so phases 5 and 6 (which currently call
`_pick_synced_image()` and grab whatever is first) can also reference the
same constants.

### Verification

After fix-3 alone, against any healthy bootstrapped server, phase 4 should
go end-to-end: kernel download → checksum → rootfs download → unsquash →
mkfs.ext4. Re-running should hit the "Rootfs already built. Skipping."
short-circuit.

### Stale Virtual Machine Image row

There is already a `ubuntu-24.04` row in the local DB with the placeholder
checksums and stale URLs. Phase 4's `_ensure_image` does
`if frappe.db.exists(...): return frappe.get_doc(...)`, so it will silently
keep using the bad row. Fix by changing `_ensure_image` to **update** the
existing row's fields from `DEFAULT_IMAGE` (using `frappe.db.set_value`
in a loop, or `doc.update(DEFAULT_IMAGE); doc.save()`). This is the right
behavior regardless: a stale fixture should not silently win over an updated
constant.

## Fix 1 — `--reuse-server` / `--keep-server` flags

### Why

Today, every phase that needs a bootstrapped server provisions a fresh
droplet (~120s create + ~60s bootstrap). Phase 4 then sync-images (~60–120s
for the v1.12 fixture). For a dev loop on phase 5 or phase 6, that's
~5 minutes of setup we don't need. The plan
([`phase-4-image-and-sync.md:166`](./phase-4-image-and-sync.md))
already names this flag; it just was never wired.

### Design

Add a single shared helper in
[`../atlas/tests/e2e/_shared.py`](../atlas/tests/e2e/_shared.py):

```python
def ensure_bootstrapped_server(
    keep: bool = False,
    reuse: bool = True,
) -> tuple["Document", "DigitalOceanClient", bool]:
    """Return an Active Server with a live droplet.

    Reuse rules:
    - If `reuse` and an Active Server row exists AND its droplet is reachable
      via a 5s SSH check, return it. (See fix 2 for the reachability check —
      this fix calls into that helper.)
    - Otherwise provision a fresh droplet via phase 3's `provision_server`
      and wait for Active.

    Returns (server_doc, do_client, created_now). `created_now=True` means
    we provisioned in this call.
    """
```

And one for image-on-server presence:

```python
def ensure_image_on_server(server_name: str) -> "Document":
    """Sync DEFAULT_IMAGE to `server_name` if not already present. Returns
    the Virtual Machine Image doc."""
```

These two helpers are the entire contract for "give me a clean stage to
test against." Phase 5 and phase 6 already implicitly want this; they just
have to know to call it.

### Where each phase calls them

| Phase | Today | After |
|-------|-------|-------|
| 3 | provisions + cleans up its own droplet | unchanged — phase 3 still tests *provisioning*, that's its point |
| 4 | `_pick_active_server` (relies on leftover) | `ensure_bootstrapped_server()`; image fixture via fix 3 |
| 5 | `_pick_active_server` + `_pick_synced_image` | `ensure_bootstrapped_server()`; `ensure_image_on_server(...)` |
| 6 | same as phase 5 | same as phase 5 |

### Flag plumbing

Pass `reuse` and `keep` via Frappe's `bench execute` kwargs:

```
bench --site atlas.local execute atlas.tests.e2e.phase_5.run \
    --kwargs '{"reuse": true, "keep": true}'
```

Each phase's `run(reuse: bool = True, keep: bool = True)` forwards both
flags into `ensure_bootstrapped_server` and skips the per-phase cleanup
when `keep=True`.

Defaults intentionally favor the warm path because CI is not a current
concern; when CI lands, override defaults there.

### What the warm path looks like

1. First-ever run (or after a manual nuke): `bench execute … phase_3.run`
   leaves nothing behind, that's still phase 3's job.
2. `bench execute … phase_4.run --kwargs '{"reuse": true, "keep": true}'`
   provisions a fresh droplet (no live one yet) → bootstraps → syncs image
   → leaves the droplet behind.
3. `bench execute … phase_5.run` (defaults) reuses the same server, image
   is already present, provision-vm in ~3s → done.
4. `bench execute … phase_6.run` (defaults) reuses the same server,
   provisions a fresh VM, exercises lifecycle.
5. When done: a single `bench execute atlas.tests.e2e._shared.teardown_all`
   helper that lists `atlas-e2e`-tagged droplets older than N min and
   prints `doctl compute droplet delete <id>` commands the operator runs
   by hand (in line with the existing
   [`_shared.sweep_old_droplets`](../atlas/tests/e2e/_shared.py#L80)
   policy of never auto-deleting tagged droplets).

### Drift from the original plan

The plan said phase 5/6 e2e "Builds on phase 4." We're formalizing that
into a shared helper instead of leaving it implicit. Log it under
[drift.md](./drift.md).

## Fix 6 — Tighter per-phase polls

### Why

Phase 4's `_wait_for_task` is 5s poll / 900s timeout. That's right for
sync-image (genuinely takes minutes). Phase 5's provision-vm takes ~3s, so
even a successful provision waits 5s before reporting. Phase 6 has the same
issue four times.

### Change

Replace the per-phase `_wait_for_task` definitions with one parameterized
helper in [`../atlas/tests/e2e/_shared.py`](../atlas/tests/e2e/_shared.py):

```python
def wait_for_task(
    task_name: str,
    timeout_seconds: int,
    poll_seconds: float = 1.0,
) -> "Document":
    """Poll a Task row to Success or Failure, or AssertionError on timeout."""
```

Caller-supplied `timeout_seconds`/`poll_seconds` per script:

| Script           | timeout | poll  |
|------------------|---------|-------|
| sync-image.sh    | 900     | 5     |
| provision-vm.sh  | 30      | 0.5   |
| start-vm.sh      | 30      | 0.5   |
| stop-vm.sh       | 30      | 0.5   |
| delete-vm.sh     | 60      | 0.5   |
| phase3-probe.sh  | 15      | 0.5   |
| phase4-probe.sh  | 15      | 0.5   |
| phase5-*.sh      | 15      | 0.5   |

The 0.5s poll on short scripts cuts a ~3s provision-vm assertion from "5s
wait" to "≤500ms wait" — small per-phase, but it compounds across phase 6
(four lifecycle Tasks per run).

### Failure-fast: detect orphans during poll

The `sleep`/`get_doc` loop should also detect a worker that died. Add:

```python
def wait_for_task(...):
    deadline = ...
    while time.monotonic() < deadline:
        frappe.db.rollback()
        task = frappe.get_doc("Task", task_name)
        if task.status in ("Success", "Failure"):
            return task
        # Orphan check: a Task stuck in Running with `started` more than
        # 2x its declared timeout ago is almost certainly orphaned. Don't
        # wait the full poll deadline for it.
        if task.status == "Running" and task.started:
            age = (frappe.utils.now_datetime() - task.started).total_seconds()
            if age > 2 * timeout_seconds:
                raise AssertionError(
                    f"task {task_name} is orphaned (Running for {age:.0f}s)"
                )
        time.sleep(poll_seconds)
    raise AssertionError(...)
```

The "2× declared timeout" threshold is conservative — `_execute_into`
internally times out at `timeout_seconds` already, so anything past 2× is
definitely a dropped worker, not a slow script.

## Fix 2 — Reconcile-before-use server check

### Why

Today's failure: the Server row said Active, the droplet was gone, every
test that called `_pick_active_server` got a doomed pointer and we waited
15 minutes to find out. The reconciliation cost is one ~5s SSH probe
**before** any phase commits to using a server.

### Design

Add to [`../atlas/tests/e2e/_shared.py`](../atlas/tests/e2e/_shared.py):

```python
def server_is_reachable(server_name: str, timeout_seconds: int = 5) -> bool:
    """Quick SSH liveness probe. Does NOT update Server.status — that's
    a separate decision the caller makes, because Active→Broken is a real
    state change with downstream consequences."""
    from atlas.atlas.ssh import connection_for_server, wait_for_ssh
    server = frappe.get_doc("Server", server_name)
    try:
        wait_for_ssh(
            connection_for_server(server),
            timeout_seconds=timeout_seconds,
            poll_seconds=1,
        )
        return True
    except Exception:
        return False
```

And the policy lives in `ensure_bootstrapped_server` (from fix 1):

```python
def ensure_bootstrapped_server(reuse=True, keep=False):
    if reuse:
        for name in frappe.get_all(
            "Server", filters={"status": "Active"}, pluck="name"
        ):
            if server_is_reachable(name, timeout_seconds=5):
                return frappe.get_doc("Server", name), get_client(), False
            # The row says Active but SSH is dead. Mark Broken so we don't
            # keep reusing it. This is the one place a phase legitimately
            # mutates Server.status — it's been verified unreachable.
            frappe.db.set_value("Server", name, "status", "Broken")
            frappe.db.commit()
            print(f"[e2e] marked {name} Broken (SSH unreachable)")
    # No reusable Active server. Provision fresh.
    ...
```

### Why not auto-mark Broken elsewhere

The codebase shouldn't get a generic "reconcile every Server row" task,
because the DO account also hosts production droplets (see
[`_shared.sweep_old_droplets`](../atlas/tests/e2e/_shared.py#L80)).
Reconciliation lives **only** in the e2e helper, where the contract is
"this row should be backing an `atlas-e2e`-tagged droplet."

### Backstop maintenance helper (one liner)

In [`../atlas/tests/e2e/_shared.py`](../atlas/tests/e2e/_shared.py), add:

```python
def mark_orphan_tasks_failure(older_than_minutes: int = 10) -> int:
    """Mark Running Tasks older than N minutes as Failure. Safety net for
    workers that died mid-job. Returns count marked."""
```

Operator runs this once after weird crashes; not auto-invoked. This caps
the blast radius of the bug we fixed today in
[`atlas/atlas/ssh.py:173`](../atlas/atlas/ssh.py#L173) — even if a future
path goes around `_execute_into`, the rows don't poison subsequent polls
forever.

## Order of operations

1. **Fix 3** — phase_4.py + _shared.py constants. Single PR, 15 minutes.
2. **Fix 1** — `ensure_bootstrapped_server` + `ensure_image_on_server` +
   wiring in phases 4/5/6, plus the `reuse`/`keep` kwargs. ~1 hour.
3. **Fix 6** — `wait_for_task` consolidated in `_shared.py`, per-script
   timeouts/polls, orphan detection in the loop. ~45 min.
4. **Fix 2** — `server_is_reachable` + the auto-Broken policy inside
   `ensure_bootstrapped_server` + `mark_orphan_tasks_failure`. ~30 min.

## Not in this plan

- No retries inside `run_task`. The spec is explicit:
  ["No automatic retries"](./00-overview.md#L161).
- No CI scaffolding. These flags optimize the dev loop; CI defaults can be
  set when CI exists (probably `reuse=False, keep=False`).
- No host-key pinning, no Server health-check reconciler, no log spill —
  all already listed under [open items intentionally deferred](./00-overview.md#L163).
- No new doctypes. Everything here is helpers in `_shared.py` plus call
  sites in `phase_N.py`.

## Verification

After all four fixes, the warm-path benchmark to target:

```
$ time bench --site atlas.local execute atlas.tests.e2e.phase_5.run
phase-5: OK in ~15s

$ time bench --site atlas.local execute atlas.tests.e2e.phase_6.run
phase-6: OK in ~30s
```

— assuming an already-bootstrapped server with the image present. The
cold-start path (no server) should be one phase-3 run followed by one
phase-4 run, ~5 min total, and that result is then reusable indefinitely
until the droplet is manually deleted.
