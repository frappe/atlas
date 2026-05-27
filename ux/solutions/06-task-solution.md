# Task — solution

Maps to [research/06-task.md](../research/06-task.md).

## 1. Task IDs are random strings, with no human-readable subject

### Problem
`8k6u4v3bi1`, `tme8q7trdq` make terrible breadcrumbs. The operator has
to read three columns (`script`, `virtual_machine`, `server`) to know
what the task did.

### Solution

Frappe doctypes can have a `title_field` separate from `name`. Add a
new computed field `subject: Data` (read-only) to the Task doctype, set
in `before_insert`:

```python
def before_insert(self):
    self.subject = self._build_subject()

def _build_subject(self) -> str:
    pretty = {
        "bootstrap-server.sh": "Bootstrap",
        "reboot-server.sh":    "Reboot",
        "sync-image.sh":       "Sync image",
        "provision-vm.sh":     "Provision VM",
        "start-vm.sh":         "Start VM",
        "stop-vm.sh":          "Stop VM",
        "restart-vm.sh":       "Restart VM",
        "terminate-vm.sh":     "Terminate VM",
    }.get(self.script, self.script)
    target = self._target_short()
    return f"{pretty} · {target}"

def _target_short(self) -> str:
    if self.virtual_machine:
        vm = frappe.db.get_value(
            "Virtual Machine", self.virtual_machine,
            ["description", "name"], as_dict=True,
        )
        return (vm.description or vm.name[:8]) + f" on {self.server}"
    return self.server or "—"
```

Then set `title_field = "subject"` on the doctype JSON. The form
breadcrumb becomes:

```
Atlas / Task / Provision VM · verify vnet_hdr fix on bootstrap-server-1779879805
```

The list view's first column ID stays the random hash (operators
sometimes need it for log-grep), but a `formatters.subject` renders the
subject as the headline.

### Wireframe

```
List view:
┌─────────────────────────────────────────────────────────────────────────────┐
│ Subject                                            Status   Script    Dur   │
│ Provision VM · verify vnet_hdr · bootstrap-…    ● Failure  prov.sh   17s   │
│ Provision VM · verify vnet_hdr · bootstrap-…    ● Success  prov.sh   22s   │
│ Terminate VM · 489d1578 · bootstrap-…           ● Success  term.sh    4s   │
│ Sync image  · bootstrap-…                       ● Success  sync.sh   3m   │
│ Bootstrap   · bootstrap-…                       ● Success  boot.sh  28s   │
└─────────────────────────────────────────────────────────────────────────────┘

Form breadcrumb:
Atlas / Task / Provision VM · verify vnet_hdr fix on bootstrap-server-1779879805
```

### Frappe components used
- `title_field` on the doctype JSON.
- One `subject: Data, read-only` field (schema change — minimal).
- `before_insert` controller hook.

### Fighting Desk?
No. `title_field` is the documented way to do this.

---

## 2. Task form is just the DocType editor

### Problem
No timeline, no diff between input and result, no link to "next task
this triggered", no Retry button, no rich error rendering. For a failed
task you can read stderr and that's it.

### Solution

The Task form gets four upgrades, all in the client script and a few
controller methods:

#### 2a. Status headline

`frm.dashboard.set_headline_alert(...)` driven by status:

- Pending: blue, "Queued — waiting for worker."
- Running: yellow + animated dot, "Running on bootstrap-server-… —
  started 12s ago."
- Success: green, "Completed in 28s. Exit code 0."
- Failure: red, "Failed in 16s. Exit code 1."

For Failure, the headline also shows the **first line of stderr** as a
hint:

```
✗ Failed in 16s. Exit code 1.
   provision-vm.sh: line 20: VIRTUAL_MACHINE_NAME: required
```

#### 2b. Header chips for related records

`frm.dashboard.add_indicator(...)` for each:

- Server (always present) — clickable → Server form.
- Virtual Machine (if `virtual_machine` is set) — clickable. Shows the
  VM's `description` not the UUID.
- Triggered by (`User`) — clickable.

This replaces the four-row top section of the current form, which is
six different fields the operator has to read.

#### 2c. Retry button

`frm.add_custom_button("Retry", retry_action)` on `status = Failure`.
Calls a new `Task.retry()` method that re-invokes `run_task(...)` with
the same `script` + `variables`, returns the new Task's name, and
routes the operator there.

Restricted to "retriable" scripts: the same operator-visible set
(`bootstrap-server.sh`, `reboot-server.sh`, `sync-image.sh`) plus
VM lifecycle scripts (they're re-runnable when the VM is in a state
that allows the corresponding lifecycle button — `provision-vm.sh`
re-runs from Pending/Failed). The Retry button calls `Task.retry()`,
which under the hood:

- For `provision-vm.sh` / `terminate-vm.sh` / `start-vm.sh` / `stop-vm.sh`
  / `restart-vm.sh`: load the linked VM and call the matching
  controller method (`vm.provision()`, etc.). This re-uses the
  validated lifecycle entry point — no duplicate state-machine logic.
- For server scripts: call `Server.run_task_dialog(script, variables)`
  with the original variables.

#### 2d. Linked-tasks panel

Tasks form a small DAG (a restart triggers two tasks; a sync chains
follow-ups). For now, link via `parent_task` — a new optional Link →
Task field. When a controller method triggers another task, it sets
`parent_task = current_task` (we'd need to thread a context here;
deferred to roadmap).

For now, a simpler win: show "Sibling tasks" — the other 4 most recent
tasks for the same `virtual_machine` (or `server` when no VM). Renders
as a small ordered list below the timing section.

### Wireframe

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ⌂ / Task / Provision VM · verify vnet_hdr fix on bootstrap-server-…      │
├──────────────────────────────────────────────────────────────────────────┤
│  Actions ▾   Retry                  (primary, red on Failure)            │
│                                                                          │
│  ✗  Failed in 16s. Exit code 1.                                          │
│     provision-vm.sh: line 20: VIRTUAL_MACHINE_NAME: required             │
│  ────────────────────────────────────────────────────────────────────── │
│                                                                          │
│  ● Server bootstrap-server-1779879805 →                                  │
│  ● Virtual Machine verify vnet_hdr fix · 8f3cf032 →                      │
│  ● Triggered by Administrator                                            │
│  ● Started 27-05-2026 17:31:17 · Ended 17:31:33                          │
│                                                                          │
│  Inputs                                                                  │
│  Script             provision-vm.sh                                      │
│  Variables          { "VIRTUAL_MACHINE_NAME": "", "VCPUS": "1", ... }    │
│                                                                          │
│  Output                                                                  │
│  ─── stderr (2.9 KB) ─────────────────────────────────────────  Copy ─  │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ + set -euo pipefail                                                │ │
│  │ /tmp/atlas/provision-vm.sh: line 20: VIRTUAL_MACHINE_NAME: required│ │
│  │ ...                                                                │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ─── stdout (0 B) ──────────────────────────────────────────────────── │
│  (empty)                                                                 │
│                                                                          │
│  Sibling tasks (same VM)                                                 │
│  ● Failure  Provision VM   17m ago    8k6u4v3bi1 →                       │
│  ● Success  Terminate VM   36m ago    ti3979a1qt →                       │
└──────────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- `frm.dashboard.set_headline_alert(html, color)`.
- `frm.dashboard.add_indicator(html, color)`.
- `frm.add_custom_button("Retry", fn)` + `frm.change_custom_button_type("Retry", null, "primary")`.
- New controller method `Task.retry()` (delegates to VM/Server methods).

### Fighting Desk?
**Mild.** The default Task form's six-field top row is exactly the
operator's pain point. We don't remove the fields (they're still in the
schema and queryable) but we **hide them on the form** via
`frm.toggle_display([list of fields], false)` in favor of the dashboard
indicators. That requires removing the right-rail and Comments panel to
breathe (see [07-cross-cutting-solution.md](./07-cross-cutting-solution.md)).

---

## 3. Stdout/Stderr are tiny clipped textareas

### Problem
8.8 KB of stdout, 2.9 KB of stderr, stuffed into ~5-line readonly
boxes with manual scroll. No log viewer affordances.

### Solution

Replace the two `Code` fields' rendering with a richer client-side
viewer, while keeping the underlying `Code` field type (so the doctype
schema doesn't change and the data round-trips cleanly).

Affordances:

1. **Bigger panel.** Set the field's `min_height` to ~24 lines via
   `frm.set_df_property("stdout", "min_height", "24em")`. (Frappe's
   Code field accepts a min-height-ish style; if not, swap to an HTML
   field that renders a `<pre>` mounted to the live value.)
2. **Monospace + ANSI colours.** Pipe the value through
   `frappe.utils.escape_html` then a small ANSI → HTML converter
   (copy 30 lines from a permissive-license library — Taste §6:
   reimplement small subsets, don't import). Render in `<pre>` with
   `white-space: pre-wrap` for the default wrap.
3. **Toolbar above each pane:**
   - `Copy`  — copies raw text to clipboard.
   - `Wrap` / `No wrap` toggle.
   - `Open in new tab` — opens
     `/api/method/atlas.api.task.stdout_raw?name=<task>` which returns
     `text/plain` with `Content-Disposition: inline` for grep/save.
   - `Search` — a small in-pane filter (filters lines client-side).
4. **Auto-tail while Running.** The Task controller emits a
   `frappe.publish_realtime("task_log", {name, stream: "stdout"|"stderr",
   chunk: "..."}, user=frm.doc.owner)` on each chunk (the SSH runner
   already reads incrementally — pipe each line through). The form
   listens via `frappe.realtime.on("task_log", append_to_pane)` and
   auto-scrolls to bottom while the user hasn't scrolled up.

### Wireframe

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Output                                                                  │
│  ─── stderr (2.9 KB) ──── Copy   Wrap   ↗ Open   🔍 ─────────────────── │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ + set -euo pipefail                                                │ │
│  │ /tmp/atlas/provision-vm.sh: line 20: VIRTUAL_MACHINE_NAME: required│ │
│  │                                                                    │ │
│  │ (… large monospace area, ~24 lines tall …)                         │ │
│  │                                                                    │ │
│  │                                                                    │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│  ─── stdout (0 B) ──── Copy   Wrap   ↗ Open   🔍 ───────────────────── │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ (empty)                                                            │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- HTML field replacing the Code field's render, **or** keep the Code
  field and override the wrapping element's CSS / replace its rendered
  child with a `<pre>` via `frm.fields_dict.stdout.$wrapper.find(...)`.
- `frappe.publish_realtime` (server) + `frappe.realtime.on` (client).
- New whitelisted Python endpoint `atlas.api.task.stdout_raw` and
  `stderr_raw`.

### Fighting Desk?
**Mild.** Desk's Code field has its own ACE/textarea renderer; we
replace the rendered child while keeping the form-binding intact. If
that proves fragile, we strip Desk just for this one field and render
our own `<pre>` from a new HTML field, with the source `Code` field
hidden.

---

## 4. No live status for in-flight tasks

### Problem
A long-running bootstrap script (28s+) is a black box. No polling, no
progress.

### Solution

`task_update` and `task_log` realtime events are introduced in the SSH
runner:

- `task_update` fires on every status transition
  (`Pending → Running → Success/Failure`). Payload:
  `{name, status, exit_code?, duration_milliseconds?}`. The Task form
  subscribes and refreshes the headline + indicator.
- `task_log` fires on each line of stdout/stderr. Payload:
  `{name, stream, chunk}`. The form appends to the right pane.

Subscriptions are room-scoped to the document
(`frappe.realtime.on("task_update:" + frm.doc.name, ...)`) so other
operators viewing other tasks aren't spammed.

The duration display becomes a live timer: while `status = Running`,
the headline shows `Running for 14s`, updated every 1s in the client.

### Wireframe

```
While running:
┌──────────────────────────────────────────────────────────────────────────┐
│  ⏵  Running on bootstrap-server-… for 14s.                              │
│  ────────────────────────────────────────────────────────────────────── │
│                                                                          │
│  Output                                                                  │
│  ─── stdout (live) ───── Copy   Wrap   ↗ Open   🔍 ─────────────────── │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │ + set -euo pipefail                                                │ │
│  │ + apt-get update                                                   │ │
│  │ Reading package lists...                                           │ │
│  │ Hit:1 https://repo.example.com/ubuntu noble InRelease              │ │
│  │ ...                                                                │ │
│  │ █ (auto-tail)                                                      │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- `frappe.publish_realtime` from the SSH runner (where each line is
  already read).
- `frappe.realtime.on` in the client script.

### Fighting Desk?
No.

---

## 5. Failed tasks leave the parent VM in `Pending` forever

### Problem
Three Pending VMs on the server, all from failed earlier provisions.
The VM form shows `Pending` but doesn't say "last provision attempt
failed — see Task tme8q7trdq."

### Solution

This is partly a VM-form concern (see
[05-virtual-machine-solution.md §3](./05-virtual-machine-solution.md#3-terminated-vm-form-is-identical-to-pending))
and partly a Task-form concern.

On the **VM form**, in the Pending intro, when there's at least one
recent Failure-status Task for this VM with `script = provision-vm.sh`:

```js
frm.set_intro(
    `Last Provision attempt failed —
     <a href="/app/task/${last_failure.name}">${last_failure.subject} →</a>
     Click Provision to retry.`,
    "red",
);
```

The intro upgrades the form from a passive doctype editor to an actor.
The operator sees the failure, follows the link, reads the error, and
clicks Provision once they've fixed it (or Terminates and re-creates
the VM if the spec was wrong).

Optionally, the VM controller can flip status to `Failed` on
provision-task failure (currently the controller sets
`status = Running` *before* the task finishes — overoptimistic):

```python
def provision(self) -> str:
    if self.status not in ("Pending", "Failed"):
        frappe.throw(f"Cannot provision from {self.status}")
    self.status = "Running"   # ← keep, optimistic
    self.last_started = frappe.utils.now_datetime()
    self.save()
    task = run_task(...)
    # After run_task synchronously completes (it's a blocking call in
    # the current codebase), check status:
    if task.status == "Failure":
        self.status = "Failed"
        self.save(ignore_permissions=True)
    return task.name
```

The Task controller's existing `task_update` realtime event also
publishes a `virtual_machine_update` event with the new VM status, and
the VM form subscribes — so an operator watching the VM form sees it
flip from Pending → Running → Failed without refreshing.

### Wireframe

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⌂ / VM / verify vnet_hdr fix · 8f3cf032            Failed   ●       │
├──────────────────────────────────────────────────────────────────────┤
│  Actions ▾   Provision (Retry)              (primary)         Save  │
│  ├ Terminate (red)                                                  │
│                                                                      │
│  ✗  Last Provision attempt failed.                                  │
│      Provision VM · 8k6u4v3bi1 →                                    │
│      Fix the cause, then click Provision to retry.                  │
│  ─────────────────────────────────────────────────────────────────  │
│                                                                      │
│  Description           Status                                       │
│  verify vnet_hdr fix   Failed                                       │
│  ...                                                                 │
└──────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- `frm.set_intro(html, "red")`.
- Controller status transition on Failure.
- `publish_realtime("virtual_machine_update", {name, status})` from
  the Task lifecycle hook.

### Fighting Desk?
No.
