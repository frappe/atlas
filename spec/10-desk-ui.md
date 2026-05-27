# Desk UI

The desk is Atlas's only UI. We don't ship a custom SPA; we lean on
Frappe's standard form, list, and dialog primitives. But every Atlas
form goes through a small layer of shared client conventions so the
operator sees a consistent action hierarchy and can't fire expensive
or destructive things by accident. This section documents what that
layer is and why it exists.

## Why deviate from Frappe defaults at all

Frappe's stock form chrome — right rail (Assign / Attachments / Tags /
Share / Last Edited By), bottom Comments / Activity panel — is built
for CRM-shaped records that humans read and annotate. Atlas records are
infrastructure: an operator reads them to act, not to comment on them.
The right rail and timeline take ~50% of the screen and contribute
nothing on a Server, VM, or Task form. So we hide them, deliberately
and per-doctype, and document the decision here so a future contributor
doesn't quietly turn them back on.

We also need a button hierarchy: a desk that renders `Save`,
`Provision`, `Terminate`, `Reboot`, `Test Connection`, `Bootstrap` as
identical pills can't communicate "this one is destructive" or "this
one costs money." Frappe supports primary / secondary / danger button
variants and button groups out of the box; we just have to use them
consistently.

## The shared client surface

One file —
[`atlas/public/js/atlas_form_overrides.js`](../atlas/public/js/atlas_form_overrides.js)
— wired via `doctype_js` for the five Atlas doctypes in
[`hooks.py`](../atlas/hooks.py). It defines `frappe.atlas.*` helpers
and applies a cross-doctype `onload` / `refresh` that strips the right
rail and timeline.

### Button-tier convention

| Tier      | Helper                       | When                                                    | Style                              |
| --------- | ---------------------------- | ------------------------------------------------------- | ---------------------------------- |
| Primary   | `frappe.atlas.add_primary`   | The single most likely action on this form/state pair   | Top bar, `btn-primary`             |
| Secondary | `frappe.atlas.add_secondary` | Frequent siblings (Restart alongside Start / Stop)      | Top bar, default                   |
| Hidden    | `frappe.atlas.add_action`    | Rare actions (Re-bootstrap on an Active server)         | Inside the `Actions ▾` group menu  |
| Danger    | `frappe.atlas.add_danger`    | Destructive (Terminate, Reboot, Delete record)          | Inside `Actions ▾`, `btn-danger`   |

Every doctype's `refresh` calls these helpers, never the bare
`frm.add_custom_button`. The convention is the convention; deviations
should be deliberate and have a reason next to them.

### Confirmation helpers

```text
frappe.atlas.confirm_cost({title, body_html, proceed_label, proceed})
frappe.atlas.confirm_destructive({title, body_html, match_string,
                                  match_label, proceed_label, proceed})
```

`confirm_cost` wraps `frappe.warn` with the orange Provision-style
indicator. Used for actions that are not destructive but spend real
money or bandwidth: Provision Server (creates a billable droplet),
Sync to All Servers (multi-GB download per target).

`confirm_destructive` is a custom dialog with a text-match input. The
red primary button stays disabled until what the operator types
matches `match_string` exactly. Used for: Reboot a server (match the
server name), Terminate a VM (match the VM's 8-char short ID), Delete
a Terminated VM record.

The match-string pattern is the same one GitHub uses for "delete
repository": the operator can't muscle-memory through it.

### Toast-and-route after every Task spawn

```text
frappe.atlas.task_started(frm, label, task_name)
```

Every controller method that returns a new Task name routes the
operator to the Task form and drops a blue toast on the source form
linking back. Latency hint copy lives inside each action's dialog
(`~90 s` for Provision Server, `~5 s` for Start, etc.) so the operator
knows what's normal.

### Chrome strip

`frappe.atlas.strip_desk_chrome(frm)`, attached to `onload` and
`refresh` for the five Atlas doctypes, hides:

- `frm.page.sidebar` — the right rail (Assign, Tags, Share, …).
- `.new-timeline` and `.comment-input-container` inside
  `frm.page.wrapper` — the activity panel and comment box.

The main column then expands from `col-lg-8` to `col-lg-12` so the
form breathes. We hide DOM nodes; we don't monkeypatch Frappe globals.

Connections dashboards (the count tiles for Workloads, Tasks, …) stay
visible — those *are* useful and Frappe renders them on the form
itself, not in the right rail.

## Per-doctype consequences

### Server Provider

- **Provision Server** is the primary action.
- **Test Connection** lives under `Actions ▾`. It's a cheap read-only
  ping; it doesn't need top-bar real estate.
- The Provision dialog shows a defaults preview block (region, size,
  monthly USD cost, image) above the Server Name field, then hands
  off to `confirm_cost` ("Create a billable droplet?"). Cost comes
  from a hand-maintained `DIGITALOCEAN_MONTHLY_COST_USD` dict — same
  policy as `default_image` (DO doesn't expose pricing per size in
  their API). Missing sizes render as "—" rather than guess.

### Server

- **Bootstrap** is primary when the server is `Pending` /
  `Bootstrapping` / `Broken`. On an Active server it folds under
  `Actions ▾` as **Re-bootstrap** — re-bootstrapping a healthy host
  is rare enough not to compete for top-bar real estate.
- **Run Task** and **Reboot** always live under `Actions ▾`.
- **Reboot** is danger. It demands the operator type the server name
  in a `confirm_destructive` dialog that also shows the running-VM
  count.

### Virtual Machine

- Lifecycle buttons follow a status-keyed hierarchy:
  - `Pending` / `Failed` → **Provision** primary.
  - `Stopped` → **Start** primary, **Restart** secondary.
  - `Running` → **Stop** primary, **Restart** secondary.
  - `Terminated` → no lifecycle buttons.
- **Terminate** is always available (until status = Terminated),
  under `Actions ▾`, danger. The `confirm_destructive` dialog shows
  IPv6, image, server, and demands the operator type the VM's 8-char
  short ID.

### Virtual Machine Image

- **Sync to Server** is the top-bar secondary action. The picker uses
  `only_select: 1` (no "+ Create a new Server" affordance) and a
  `status = Active` filter — syncing to a Pending/Bootstrapping server
  is wrong because the bootstrap installs Firecracker and the sync
  target directory.
- **Sync to All Servers** lives under `Actions ▾`. Before fanning
  out it shows a `confirm_cost` dialog listing the active servers and
  reminding the operator each download fetches kernel + rootfs from
  the public internet.

### Task

- The form is read-only (`disable_save()`).
- Status-coloured dashboard headline:
  - Pending → blue, "Queued — waiting for worker."
  - Running → yellow, "Running on <server> — started 12s ago."
  - Success → green, "Completed in 28s. Exit code 0."
  - Failure → red, "Failed in 16s. Exit code 1." + the first
    non-trace stderr line as a one-line hint.
- Header chips for the related Server, Virtual Machine, and
  triggered-by User. VM is shown by description, not UUID.
- **Retry** button (primary) when status = Failure. Delegates to the
  matching VM controller method (`provision()`, `start()`,
  `terminate()`, …) for VM-scoped scripts, or to
  `Server.run_task_dialog(...)` for server-scoped scripts. The
  state-machine guards live in those methods — the Retry button does
  not duplicate them.
- "Sibling tasks" — the most recent four other Tasks for the same VM
  (or Server when the Task has no VM) — so the operator can hop
  between Tasks for one workload without navigating through the VM
  form.

## Why this isn't a custom SPA

Every win above lives in a Frappe `Dialog`, a button group, a form
intro, a dashboard indicator, or a `doctype_js` client script. We
don't replace the Desk form. We don't add a route. We don't add a
build step. The whole thing is Desk plus ~300 lines of shared client
JS, and a couple of whitelisted controller methods (`preview_cost`,
`retry`, `operator_visible_scripts`).

The two places we explicitly fight Desk are documented at the call
site: the chrome strip (right rail + timeline) on every form, and the
Task form's read-only/headline override that suppresses the standard
six-field top row in favor of the dashboard headline + chips. Both
are intentional; both are reversible by removing one client script.
