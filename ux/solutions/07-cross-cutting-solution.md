# Cross-cutting — solution

Maps to [research/07-cross-cutting.md](../research/07-cross-cutting.md).

These fixes apply uniformly to every Atlas form. They are the
"infrastructure" the per-doctype solutions reference.

## 1. Desk's default DocType chrome is fighting Atlas

### Problem
Every form has a right rail (`Assign` / `Attachments` / `Tags` / `Share`
/ `Last Edited By` / `Created By`) and a bottom `Comments` panel that
Atlas doesn't need. The operationally-relevant content (status,
buttons, key fields) occupies maybe 50% of the screen.

### Solution

**Strip Desk** at the doctype level, in three steps. Each step is an
explicit "we don't need this" decision; Frappe supports each one
natively.

#### 1a. Disable Assign / Track / Comments per-doctype

Set doctype JSON flags:

```json
{
  "track_changes": 0,            // turn off the Activity tab
  "track_seen": 0,
  "track_views": 0,
  "allow_import": 0,
  "allow_rename": 0,
  "is_calendar_and_gantt": 0,
  "quick_entry": 0
}
```

Comments and tags also obey `hide_toolbar`. For each of `Server`,
`Server Provider`, `Virtual Machine`, `Virtual Machine Image`, `Task`,
set `hide_toolbar: 0` (keep the toolbar — we need our buttons) but
suppress Comments via a small client script:

```js
frappe.ui.form.on(<doctype>, {
    onload(frm) {
        frm.timeline?.timeline_wrapper?.hide();   // comments + activity
        frm.sidebar?.hide();                      // right rail
    },
});
```

`frm.sidebar.hide()` is a single-line Desk override; we apply it
across the five doctypes via a shared client script bundled in
`atlas/public/js/atlas_form_overrides.js` and registered in
`hooks.py`:

```python
doctype_js = {
    "Server":                "public/js/atlas_form_overrides.js",
    "Server Provider":       "public/js/atlas_form_overrides.js",
    "Virtual Machine":       "public/js/atlas_form_overrides.js",
    "Virtual Machine Image": "public/js/atlas_form_overrides.js",
    "Task":                  "public/js/atlas_form_overrides.js",
}
```

The script is small (~30 lines) and uses only documented `frm.*`
hooks — it doesn't monkeypatch Frappe globals.

#### 1b. Keep what we need

We deliberately keep:

- **Connections dashboard** (Workloads / Tasks panels on Server, Image).
- **Status pill** in the breadcrumb (Frappe auto-renders it from
  `Select` fields with the `status` fieldname).
- **Saved Filters** in list views.

#### 1c. Document the stripping

Add a section to `spec/01-architecture.md` (or a new
`spec/10-desk-ui-decisions.md`) recording **why** we hide the sidebar
and Comments per doctype. This is a deviation from Frappe's defaults;
without a paper trail, a future contributor will turn them back on.

### Wireframe

```
Before:                                                After:
┌───────────────────────────────────────────────┐    ┌───────────────────────────────────────────────┐
│  Form content     │ Assign            │ ... │    │  Form content                                │
│  (50% width)      │ Attachments       │     │    │  (full width)                                │
│                   │ Tags              │     │    │                                              │
│                   │ Share             │     │    │                                              │
│                   │ Last Edited By    │     │    │                                              │
│                   │ Created By        │     │    │                                              │
└───────────────────┴───────────────────┴─────┘    └───────────────────────────────────────────────┘
                                                    (Comments / timeline also hidden.)
```

### Frappe components used
- `frm.sidebar.hide()`, `frm.timeline.timeline_wrapper.hide()` — both
  documented `frm.*` hooks.
- `hooks.py` → `doctype_js`.

### Fighting Desk?
**Yes — deliberately.** This is exactly the "strip Desk" item flagged
at the top of the cross-cutting research. We don't modify Frappe core;
we hide DOM elements at form load. Documented decision lives in the
spec.

---

## 2. No primary-action / dangerous-action visual hierarchy

### Problem
`Save`, `Provision`, `Terminate`, `Reboot`, `Test Connection`,
`Bootstrap` all render as identical pills.

### Solution

Adopt a project-wide button-tier convention used in every per-doctype
solution file:

| Tier      | Style                            | When                                                    |
| --------- | -------------------------------- | ------------------------------------------------------- |
| Primary   | Dark / coloured top-bar pill     | The single most likely action on this form/state pair   |
| Secondary | Default top-bar pill             | Frequent actions (Restart, Start, Stop)                 |
| Hidden    | Under `Actions ▾` button-group   | Rare actions (Re-bootstrap, Run Task)                   |
| Danger    | Red, inside `Actions ▾`          | Destructive (Terminate, Reboot, Delete record)          |

Implementation lives in the shared client script:

```js
frappe.atlas = frappe.atlas || {};

frappe.atlas.add_primary = (frm, label, fn) => {
    frm.add_custom_button(__(label), fn);
    frm.change_custom_button_type(__(label), null, "primary");
};

frappe.atlas.add_secondary = (frm, label, fn) => {
    frm.add_custom_button(__(label), fn);
};

frappe.atlas.add_action = (frm, label, fn) => {
    frm.add_custom_button(__(label), fn, __("Actions"));
};

frappe.atlas.add_danger = (frm, label, fn) => {
    frm.add_custom_button(__(label), fn, __("Actions"));
    frm.change_custom_button_type(__(label), __("Actions"), "danger");
};
```

Every doctype's `refresh` handler calls these helpers, never the bare
`frm.add_custom_button`. The convention is enforced by code review and
documented in `spec/10-desk-ui-decisions.md`.

### Wireframe

See per-doctype solution files; the pattern repeats.

### Frappe components used
- `frm.add_custom_button` + `frm.change_custom_button_type` + button
  group.

### Fighting Desk?
No.

---

## 3. No confirmations on anything destructive or expensive

### Problem
Provision Server, Sync to All Servers, Reboot, Terminate — one click.

### Solution

Two reusable client-side helpers, both wrappers around standard Frappe
primitives:

```js
frappe.atlas.confirm_cost = ({title, body_html, proceed_label, proceed}) => {
    // Wraps frappe.warn with orange indicator + Provision-style copy.
    return frappe.warn(title, body_html, proceed, proceed_label, true);
};

frappe.atlas.confirm_destructive = ({title, body_html, match_string, proceed_label, proceed}) => {
    // Custom dialog with text-input confirmation. Disables the red
    // primary button until the input matches `match_string`.
    ...
};
```

Apply across:

- **Provision Server** → `confirm_cost` (see Provider §2).
- **Sync to All Servers** → `confirm_cost` (see Image §2).
- **Reboot** → `confirm_destructive` with server name (see Server §5).
- **Terminate** → `confirm_destructive` with VM short id (see VM §4).
- **Delete (Terminated) VM** → `confirm_destructive` with VM short id.

Each call site picks the helper based on the action's nature.

### Wireframe

See per-doctype wireframes.

### Frappe components used
- `frappe.warn`.
- `frappe.ui.Dialog` (custom variant for typed confirmation).

### Fighting Desk?
No.

---

## 4. No idea of "what's normal"

### Problem
- No latency expectations on dialogs ("provisioning takes ~90s").
- No progress indicator while a long action runs.
- No toast linking to the resulting record after a click.

### Solution

Three project-wide micro-conventions:

#### 4a. Latency hints inside every action dialog

Every dialog whose primary action triggers a multi-second remote call
includes a footer line:

> _Provisioning takes ~90 seconds. The new Server form opens
> automatically and the bootstrap Task runs in the background._

The hint lives as an `HTML` field at the bottom of the dialog. Times
are operator-facing approximations; we round generously upward (people
forgive slow more than wrong-ETA).

| Action               | Hint                                     |
| -------------------- | ---------------------------------------- |
| Provision Server     | ~90 s — DO + bootstrap                   |
| Bootstrap            | ~30 s                                    |
| Reboot               | ~60 s — SSH drops; Task may end Failure  |
| Sync to Server       | ~3 min per server (image size dependent) |
| Provision VM         | ~30 s                                    |
| Start / Stop / Restart| ~5 s                                    |
| Terminate VM         | ~5 s                                     |

#### 4b. Progress indicator while a long action runs

Use the Task realtime events from
[06-task-solution.md §4](./06-task-solution.md#4-no-live-status-for-in-flight-tasks).
The parent form (Server / Image / VM) listens for the spawned Task's
`task_update` event and renders a yellow headline:

```
⏵  Provision VM running on bootstrap-server-… for 14s. Watch live →
```

Click → opens the Task form.

#### 4c. Toast linking to the resulting record

Already partially in place (`frappe.show_alert` after each call). Make
it consistent: every controller method that returns a Task name
**always** routes the operator to the Task form **and** drops a
breadcrumb toast on the source form:

```js
frappe.atlas.task_started = (frm, label, task_name) => {
    frappe.show_alert({
        message: `${label} Task: <a href="/app/task/${task_name}">${task_name}</a>`,
        indicator: "blue",
        duration: 6,
    });
    frappe.set_route("Form", "Task", task_name);
};
```

### Wireframe

```
After clicking a button that spawns a Task:
┌──────────────────────────────────────────────────────────────────────┐
│  ╭───────────────────────────────────────────────────────╮          │
│  │ Provision VM Task: 8k6u4v3bi1                  ✕     │          │
│  ╰───────────────────────────────────────────────────────╯          │
│                                                                      │
│                       (form auto-routes to the Task in ~200ms)      │
└──────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- `frappe.show_alert`.
- `frappe.set_route`.
- Dialog HTML footer.

### Fighting Desk?
No.

---

## 5. Operations and audit are separated

### Problem
The Server form has back-links to Task and VM, but the Task list has no
back-link to the VM (the `virtual_machine` column is there but doesn't
render for the failed task example).

### Solution

This is a data-display bug at the list view layer. Two fixes:

#### 5a. Task list always shows the `virtual_machine` link

The `virtual_machine` field already exists in the schema. The screenshot
shows it appearing for some rows and not others — depending on whether
the row's source variables included a VM. For tasks where it's empty
(`bootstrap-server.sh`, `sync-image.sh`), the column is blank and
that's correct.

For tasks where the VM is set, render it as the VM's `description` not
the UUID — same pattern as the subject field:

```js
frappe.listview_settings["Task"].formatters = {
    virtual_machine(value) {
        if (!value) return "";
        return frappe.utils.get_link_to_form(
            "Virtual Machine", value,
            (frappe._cached_vm_descriptions || {})[value] || value.slice(0, 8),
        );
    },
};
```

The description map is loaded once per session via a small whitelisted
helper.

#### 5b. Bidirectional connections dashboard on every doctype

The spec's connections wiring is already one-way (parent → children).
Add reverse-direction dashboards:

- **Task** → its `Server` and `Virtual Machine` parents. These already
  show as link fields; promote them into the **header chips** (see
  [06-task-solution.md §2b](./06-task-solution.md#2b-header-chips-for-related-records)).
- **Virtual Machine** → already has a Task connections panel via
  `virtual_machine_dashboard.py`. Confirm the data wiring renders.

For VM ↔ Task, the bidirectional bonus is the **Sibling tasks** panel
from [06-task-solution.md §2d](./06-task-solution.md#2d-linked-tasks-panel)
which lets the operator hop between Tasks for the same VM without
going through the VM form.

### Wireframe

```
Task form, header chips:                  Task list, virtual_machine column:
● Server bootstrap-server-… →             Server     VM (description)  Script
● VM verify vnet_hdr · 8f3cf032 →         bootstrap- verify vnet_hdr   provision-vm.sh
● Triggered by Administrator              bootstrap- 489d1578          terminate-vm.sh
                                          bootstrap- (empty)           sync-image.sh
                                          bootstrap- (empty)           bootstrap-server.sh
```

### Frappe components used
- `frappe.listview_settings.formatters`.
- `frappe.utils.get_link_to_form`.

### Fighting Desk?
No.
