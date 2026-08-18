# Observability — making long-running tasks legible

Some operator actions finish in milliseconds (start/stop a VM). A handful take
minutes, and one takes the better part of ten:

| Operation | Entry point | Wall-clock | Today's surface |
| --------- | ----------- | ---------- | --------------- |
| **Bake an image** | `Image Build` → New / Re-bake, or `Server` → Bake Image | provision build VM (~10 min on a cold account) → `build.sh` in-guest (~5–15 min) → snapshot | `Image Build.status` (4 broad states) + `image_build_progress` realtime |
| **Bake a proxy** | `proxy.build_proxy(vm)` (TLS / proxy setup) | nginx + luajit compile in-guest (~10–20 min) | one Task row, written **on completion** |
| **Provision a server** | `Atlas Settings` → Provision Server → `finish_provisioning` | vendor create → wait active → wait SSH → bootstrap (~10 min) | `Server.status` (3 states), **no realtime** |
| **Sync an image to a server** | `Virtual Machine Image` → Sync to Server | download ~600 MB → sha256 → unsquash → mkfs (up to 900s) | Task row + `task_update` realtime |
| **Issue a TLS cert** | `Root Domain` → Issue / Renew | ACME → DNS-01 propagation → certbot → push to proxies | Task row (`issue-cert.py`) |
| **Deploy a self-serve site** | `create_site` → `auto_provision` chain | warm clone + deploy (~3–5 min) | per-step Task rows |

The common defect is the same in every row: the operator clicks, the doc flips
to a coarse in-progress state (`Building`, `Bootstrapping`, `Running`), and then
**nothing** changes for many minutes. There is no way, from Desk, to tell a bake
that is on step 2 of `build.sh` from one that has wedged — short of SSHing the
host and reading `journalctl` (the [README non-goal](./README.md#non-goals-this-iteration)
"`journalctl` is enough" was true for a fleet of fast Tasks; it is not enough for
a ten-minute bake an operator is watching). This document specifies the surface
that closes that gap.

## The model: the Task row is the live progress carrier

**Every** long operation already lands a Task ([04-tasks.md](./04-tasks.md)):
`bootstrap-server.py`, `sync-image.py`, the in-guest `bench-build` / `proxy-build`
synthetic Tasks, `issue-cert.py`, `deploy-site`. The Task is therefore where live
progress rides — not a new parallel doctype. We extend the existing Task lifecycle
in two ways, both additive:

1. **Live log streaming** — the running script's combined stdout/stderr is
   streamed onto the Task row *as it runs*, not written once when it exits.
2. **Status realtime everywhere** — the two transitions that today commit
   silently (server provisioning, proxy bake) publish on the same realtime
   channels Image Build and Task already use, so a desk form watching the doc
   updates without a refresh.

No new percentages, no estimated-time-remaining, no fabricated progress bars. A
ten-minute bake has no honest percentage; what the operator actually wants is the
**live tail of what the build is doing right now** plus the assurance that bytes
are still moving. Streaming the log delivers exactly that.

### Why streaming, and why it is nearly free here

The long in-guest builds already run **detached** through
[`run_detached`](../atlas/atlas/_ssh/transport.py) (`bench-build`, `proxy-build`),
which tees combined output to a remote `log_path` and **polls** the guest until a
`done_path` marker appears. Today the poll reads the log exactly once — `cat
log_path` *after* `done_path` exists. The whole streaming mechanism is one change
to that loop: on each poll, read the log's **new tail** (track a byte offset;
`tail -c +<offset>`), and if it grew, append it to the Task and publish it. The
build is already writing the file every poll interval; we are only choosing to
read it sooner.

For the **foreground** SSH Tasks (`sync-image.py`, `bootstrap-server.py`), which
run through [`run_ssh`](../atlas/atlas/_ssh/transport.py) with `capture_output`,
output is buffered until the process exits. Streaming those means reading the
child's pipes incrementally instead of `capture_output=True` (a
`subprocess.Popen` with a line-reader thread, or `tail`-on-the-guest as the
detached path does). This is the larger of the two implementation steps; it is
scoped to `transport.py` and changes no Task contract.

### The Task fields this adds

Two fields on the `Task` doctype, both operator-read-only:

- **`live_output`** (`Code`/`Long Text`) — the streamed combined log while the
  Task is `Running`. On completion it is superseded by the authoritative
  `stdout` / `stderr` that `_finalize` already writes (the live buffer is a tail
  for watching; the final fields are the full audit record). A bounded buffer —
  keep the **last N KB** — so a chatty `bench init` does not bloat the row; the
  full output still lands in `stdout` at the end.
- **`progress_line`** (`Data`) — the most recent non-empty line of the live log,
  surfaced as a one-line "what's happening now" in list views and dashboard
  chips, where the full `live_output` is too heavy to show.

Both are denormalized, throwaway-while-running views. The source of truth on
completion is unchanged: `stdout`, `stderr`, `exit_code`, `status`.

### Realtime channel

The streaming reuses the Task's existing realtime seam. `Task._publish_update`
([task.py](../atlas/atlas/doctype/task/task.py)) already publishes a `task_update`
event to the **doc room** on every save. We add a **second, lighter event** for
the high-frequency log appends so we don't re-serialize the full status payload
on every poll:

```
frappe.publish_realtime(
    event="task_log",
    message={"name": task.name, "append": new_tail, "progress_line": last_line},
    doctype="Task", docname=task.name,
)
```

`task_update` keeps firing on the real lifecycle transitions
(`Pending`→`Running`→`Success`/`Failure`); `task_log` carries the between-state
streaming. The Task form subscribes to both: `task_update` repaints the status
pill, `task_log` appends to the live-output panel.

The publish cadence is bounded — at most one `task_log` per poll interval (the
detached path already polls every ~10s; the foreground path debounces to the
same order). The point is liveness, not a terminal emulator.

## Closing the per-operation gaps

With Task-level streaming in place, each long operation inherits live output for
free, because each already runs through a Task. The remaining work is the
**status realtime** the non-Task transitions skip:

- **Image bake** — already publishes `image_build_progress` on each status
  transition ([image_build.py](../atlas/atlas/doctype/image_build/image_build.py),
  `_set_status`). With the linked `build_task` now streaming, the operator on the
  Image Build form sees both the coarse stage *and* the live `build.sh` tail. No
  change beyond surfacing the linked Task's `live_output` on the form.
- **Proxy bake** — runs `image_builder.run_build`, identical detached mechanics
  to the bench bake, so the streamed `proxy-build` Task is the live surface. The
  gap it closes: today the proxy Task is written *on completion* only — with
  streaming it appears `Running` immediately and tails live.
- **Server provisioning** — `finish_provisioning`
  ([worker.py](../atlas/atlas/doctype/server/worker.py)) commits each
  `Server.status` transition but **publishes nothing**. Add the same
  publish-after-commit `_set_status` shape Image Build uses:

  ```
  frappe.publish_realtime(
      event="server_update",
      message={"name": server.name, "status": status},
      doctype="Server", docname=server.name,
  )
  ```

  The `bootstrap-server.py` Task it spawns streams live; the surrounding
  vendor-create / wait-active / wait-ssh phases are coarse status transitions
  (there is no log to stream for "waiting for the vendor"), so for those the
  status event is the right granularity.
- **Image sync, TLS issue, site deploy** — all already Task-backed; they inherit
  streaming with no controller change.

## The fleet view: "what is running right now"

Per-doc liveness answers "is *this* bake alive". The operator also needs the
fleet answer — "what long operations are in flight across the whole fleet" —
without opening each form. This is a single Desk surface, in keeping with
[operating principle 1](./README.md#operating-principles) (Desk is the operator
UI; no custom pages):

- A **"Running Operations"** list/report — every Task in `Pending` / `Running`,
  plus any `Image Build` not in a terminal state, plus any `Server` in
  `Pending` / `Bootstrapping` — each row showing its `progress_line` and elapsed
  time. A Workspace shortcut on the Atlas workspace links it.

This is a saved filter + a `progress_line` column, not new machinery. It is the
"is the queue moving" pane the spec's
[queued-task ownership](./04-tasks.md#queued-task-ownership) section gestures at
("visible enough that the operator notices") — made into a first-class view.

## What this is not

- **Not metrics or alerting.** This is operator-facing *liveness* for actions an
  operator triggered and is watching, not host telemetry, time-series, or paging —
  the narrower "make the thing I clicked legible while it runs."
- **Not a new doctype.** Progress rides the existing Task row and the existing
  `image_build_progress` / `task_update` realtime channels.
- **Not fabricated progress.** No percentage or ETA on operations whose step
  count or duration we cannot honestly bound. The live log tail is the honest
  signal; status transitions are the coarse one.
- **Not a guest agent.** Streaming reads the same remote log the detached build
  already tees, over the same SSH transport. No process runs on the server that
  was not already running ([README principle 5](./README.md#operating-principles)).

## Testing

The streaming seam is a host fact in the same sense the SSH transport is: only a
real (or faked) long-running remote command proves the tail advances. The
unit-coverable half — offset tracking, the bounded-buffer truncation, the
`progress_line` extraction, the debounce — is pure string/offset logic and tests
in milliseconds with no host, like the rest of `_ssh`. The host fact (live tail
advances during a real detached build, final `stdout` matches the streamed
union) rides the existing `bench_image` / `proxy_vm` e2e modules
([README § Testing](./README.md#testing)), which already bake inside a guest —
they assert the streamed `live_output` is non-empty mid-build and that the
finalized `stdout` is the authoritative full log.
