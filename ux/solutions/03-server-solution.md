# Server — solution

Maps to [research/03-server.md](../research/03-server.md).

## 1. Equal-weight top-bar buttons hide intent

### Problem
`Bootstrap`, `Run Task`, `Reboot` all render identically. Bootstrap is
one-time, Reboot is occasional, Run Task is a power-user escape hatch.

### Solution

Apply a three-tier action hierarchy, the same pattern used everywhere
else in this design:

| Tier      | Button group        | Examples                          | Reasoning                                  |
| --------- | ------------------- | --------------------------------- | ------------------------------------------ |
| Primary   | top bar, primary    | (none — Server has no "main" act) | Server is mostly inspected, not acted on   |
| Secondary | top bar, default    | `Bootstrap` (only if Pending/Broken)| Conditional; appears only when relevant   |
| Hidden    | `Actions ▾` group   | `Run Task`, `Reboot`              | Rare/destructive; live behind one click   |

`Bootstrap` is conditionally shown based on `status` — once a server is
`Active` the button vanishes. Re-bootstrapping a healthy server is rare
enough to live under `Actions ▾`.

```js
const status = frm.doc.status;
if (["Pending", "Bootstrapping", "Broken"].includes(status)) {
    frm.add_custom_button(__("Bootstrap"), bootstrap_action);
    frm.change_custom_button_type(__("Bootstrap"), null, "primary");
} else {
    frm.add_custom_button(__("Re-bootstrap"), bootstrap_action, __("Actions"));
}
frm.add_custom_button(__("Run Task"), open_run_task_dialog, __("Actions"));
frm.add_custom_button(__("Reboot"), reboot_action, __("Actions"));
frm.change_custom_button_type(__("Reboot"), __("Actions"), "danger");
```

### Wireframe

```
Status = Pending:                          Status = Active:
┌────────────────────────────────────┐    ┌────────────────────────────────────┐
│  Actions ▾   Bootstrap   …  Save   │    │  Actions ▾                   Save  │
│  ├ Run Task              (primary) │    │  ├ Re-bootstrap                    │
│  └ Reboot (red)                    │    │  ├ Run Task                        │
└────────────────────────────────────┘    │  └ Reboot (red)                    │
                                          └────────────────────────────────────┘
```

### Frappe components used
- `frm.add_custom_button(label, fn, group)` + `frm.change_custom_button_type`.

### Fighting Desk?
No.

---

## 2. No "what's running right now?" panel

### Problem
The form shows infrastructure facts; the operator has to click into the
Task list and filter to see recent activity.

### Solution

Add two surfaces on the Server form:

1. **Dashboard headline** — when there's an in-flight Task on this
   server, render a yellow indicator at the top of the form:
   `frm.dashboard.set_headline_alert("Running task: <script> (started 12s ago) →", "yellow")`.
   Clickable; routes to the Task form.
2. **"Recent activity" connections section** — extend
   `server_dashboard.py` to include a `non_standard_fieldnames` block
   that surfaces the **last 5 Tasks for this server**, with their
   status indicator and the elapsed time. The Connections dashboard
   already renders these as count tiles; we override the per-tile
   rendering for "Task" to show recent rows inline instead of just a
   count.

The headline is updated by `frappe.realtime.on("task_update", ...)` —
when a Task transitions to `Running`/`Success`/`Failure` on this
server, the listener refetches the headline.

### Wireframe

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⌂ / Server / server-blr1-01                              Active  ●   │
├──────────────────────────────────────────────────────────────────────┤
│ ⏵  Running task: provision-vm.sh  (started 12s ago) →                │
│ ─────────────────────────────────────────────────────────────────── │
│                                                                      │
│ Operations                                                           │
│ ┌──────────────────────────────┐  ┌──────────────────────────────┐  │
│ │ Virtual Machines          4  │  │ Recent Tasks                 │  │
│ │  +  Add Virtual Machine      │  │  ● Success  provision-vm 35m │  │
│ └──────────────────────────────┘  │  ● Success  terminate    36m │  │
│                                   │  ● Failure  provision-vm 41m │  │
│                                   │  ● Success  bootstrap    1h  │  │
│                                   │                  View all →  │  │
│                                   └──────────────────────────────┘  │
│                                                                      │
│ Provider *         Status *                                          │
│ ...                                                                  │
└──────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- `frm.dashboard.set_headline_alert(html, color)`.
- Existing `frm.dashboard` connections dashboard (already wired in
  `server_dashboard.py`); a small render override turns the Task tile
  into a list.
- `frappe.realtime.on("task_update", ...)`.

### Fighting Desk?
**Mild.** The default Connections dashboard renders counts only; we
override the Task tile to render rows. The override is local to the
Server form's client script so it doesn't affect any other doctype.

---

## 3. `Run Task` dialog is the worst offender

### Problem
The Select exposes nine scripts. Only `bootstrap-server.sh`,
`reboot-server.sh`, `sync-image.sh` belong here; the rest are internal
state-machine moves that must be triggered from the VM/Image
controllers. With empty `Variables (JSON): {}` the operator can fire
`terminate-vm.sh` from this menu and crash with an opaque error.

### Solution

This is the single biggest UX win in the app. Three parts:

#### 3a. Whitelist operator-visible scripts

`scripts_catalog.allowed_scripts()` today lists every `.sh` in
`scripts/`. Split into two functions:

```python
OPERATOR_VISIBLE = {
    "bootstrap-server.sh",
    "reboot-server.sh",
    "sync-image.sh",
}

def allowed_scripts() -> list[str]:
    """All scripts that can be invoked on a server (used by SSH runner)."""
    ...  # current behavior

def operator_visible_scripts() -> list[str]:
    """Scripts the Run Task dialog is allowed to expose."""
    return sorted(
        name for name in allowed_scripts() if name in OPERATOR_VISIBLE
    )
```

Server's `get_scripts()` returns `operator_visible_scripts()`. The
controller's `run_task_dialog` still validates against the broader
`allowed_scripts()` so the existing SSH path keeps working — but the
desk picker shrinks to three entries.

#### 3b. Per-script forms

The Run Task dialog renders a different field set per script. Replace
the raw `Variables (JSON)` Code field with a script-aware form:

| Script               | Fields                                                        |
| -------------------- | ------------------------------------------------------------- |
| `bootstrap-server.sh`| `FIRECRACKER_VERSION` (Data, default `v1.15.1`), `ARCHITECTURE` (Select, options `x86_64\naarch64`, default `x86_64`) |
| `reboot-server.sh`   | (no fields — but typed confirm, see §5)                       |
| `sync-image.sh`      | `Image` (Link → Virtual Machine Image, required)              |

The mapping lives in a small client-side dict next to the dialog
opener; on `Script` change the dialog calls `dialog.set_fields(...)`
(supported on `frappe.ui.Dialog`) with the matching field list.

The submitted `Variables (JSON)` is built client-side from the typed
fields and posted to the existing `run_task_dialog` method — no server
change needed beyond the visibility split.

#### 3c. "Advanced" escape hatch (System Manager only)

For developer debugging, keep a `Show advanced` toggle inside the Run
Task dialog. When checked, the original raw-JSON textarea reappears and
the Script Select expands to `allowed_scripts()`. Visible only when
`frappe.user_roles.includes("System Manager")`. This preserves the
"hand-fire a script with custom vars" capability the spec already
relies on for debugging while keeping the default safe.

### Wireframe

```
┌──────────────────────────── Run Task ─────────────────────────────────┐
│                                                                       │
│  Script *                                                            │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │  bootstrap-server.sh                                       ▾ │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  Firecracker Version          Architecture                            │
│  ┌─────────────────┐          ┌─────────────────┐                    │
│  │ v1.15.1         │          │ x86_64        ▾ │                    │
│  └─────────────────┘          └─────────────────┘                    │
│                                                                       │
│  ⓘ Idempotent. Safe to re-run on an Active server.                   │
│                                                                       │
│  □  Show advanced  (System Manager)                                  │
│                                                                       │
│                                            [ Cancel ]   [ Run → ]    │
└───────────────────────────────────────────────────────────────────────┘

if Script = sync-image.sh:                if Script = reboot-server.sh:
  Image *  [ubuntu-24.04 ▾]                  (no fields; goes straight to
                                              typed-confirm — see §5)
```

### Frappe components used
- `frappe.ui.Dialog.set_fields([...])` to re-render on script change.
- `frappe.user_roles` for the advanced toggle.
- Existing `Server.run_task_dialog` whitelisted method (unchanged).
- New `scripts_catalog.operator_visible_scripts()` helper.

### Fighting Desk?
No.

---

## 4. `Variables (JSON)` is a raw textarea

### Problem
No schema, no hint of what keys each script wants.

### Solution

Replaced entirely by §3b. The default dialog never shows raw JSON; the
advanced toggle (System Manager only) brings it back.

For the spec's "desk-button coverage" test that exercises the
JSON-string-vs-dict path: the controller's existing handling stays. The
test in `desk_buttons.py` still sends a JSON string; the new dialog
path sends an object built from typed fields. Both shapes are still
accepted.

---

## 5. `Reboot` has no confirmation

### Problem
One click reboots a production VM host.

### Solution

Use `frappe.warn` (red confirmation) with a typed name confirmation —
the same pattern Linear/GitHub use for "delete repository". The body:

```
This will reboot bootstrap-server-1779879805 (running 4 virtual machines).
All VMs will lose connectivity until the host returns.

Type the server name to confirm:
┌─────────────────────────────────────────────┐
│                                             │
└─────────────────────────────────────────────┘
```

The number of running VMs comes from
`frappe.db.count("Virtual Machine", {"server": name, "status": "Running"})`,
fetched in `before_show`.

### Wireframe

```
┌────────────────────────────── Confirm reboot ─────────────────────────┐
│ ⚠   Reboot bootstrap-server-1779879805?                                │
│                                                                       │
│ This server is running 4 virtual machines. All will lose connectivity │
│ until the host returns. SSH will drop mid-Task — the reboot Task may  │
│ end with Status = Failure; that is normal.                            │
│                                                                       │
│ Type the server name to confirm:                                      │
│ ┌───────────────────────────────────────────────────────────────────┐│
│ │ bootstrap-server-1779879805                                       ││
│ └───────────────────────────────────────────────────────────────────┘│
│                                                                       │
│                                       [ Cancel ]   [ Reboot ]         │
│                                                       (red, disabled  │
│                                                        until match)   │
└───────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- Custom dialog (not `frappe.warn` directly, because we need a text
  input) — but **styled** the same way: `indicator: "red"`,
  primary button uses `btn-danger`. The pattern is in
  `frappe/public/js/frappe/ui/messages.js`.
- `frappe.db.count` for the VM count.

### Fighting Desk?
No.

---

## 6. Sidebar Operations panel counts are misleading

### Problem
The Connections sidebar shows `Task 9` — a session-wide count, not
in-flight. Useless at a glance.

### Solution

Two changes to `server_dashboard.py`:

```python
def get_data():
    return {
        "fieldname": "server",
        "transactions": [
            {
                "label": _("Workloads"),
                "items": ["Virtual Machine"],
            },
            {
                "label": _("In-flight Tasks"),
                "items": ["Task"],
                # New: pass a default filter for the count badge.
                "default_filters": {"status": ["in", ["Pending", "Running"]]},
            },
        ],
    }
```

Frappe doesn't natively support `default_filters` on the Connections
dashboard count badge today — so the implementation either:

- **Stays standard** by relabelling the section "Tasks" and showing the
  total (operator learns to click in for in-flight), **or**
- **Strips Desk a tiny amount** by extending `server_dashboard.py` to
  emit an HTML block (already supported) for "In-flight Tasks: 0" with
  a count drawn from `frappe.db.count("Task", {server: name,
  status: ["in", ["Pending", "Running"]]})`.

Pick the HTML-block approach. It's local to the Server dashboard and
doesn't fight Desk's defaults — Desk just renders the HTML we hand it.

### Wireframe

```
┌──────────────────────────────────────────────────────────────────────┐
│ Operations                                                           │
│ ┌──────────────────────────────┐  ┌──────────────────────────────┐  │
│ │ Virtual Machines        4    │  │ Tasks                   9    │  │
│ │  +  Add Virtual Machine      │  │  In-flight              0    │  │
│ └──────────────────────────────┘  │  Failed (24h)           1    │  │
│                                   └──────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- Connections dashboard transactions (existing).
- Extra HTML block rendered next to it for "In-flight"/"Failed (24h)"
  counts.

### Fighting Desk?
No — we add markup, we don't remove or override.
