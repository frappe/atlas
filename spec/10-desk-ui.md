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

## The workspace

The Atlas workspace is the operator's home. It is restructured around three
sections, top-to-bottom:

1. **Bootstrap checklist** — a Custom HTML Block shipped as a fixture
   (`atlas-bootstrap-checklist`) whose script calls
   `atlas.atlas.api.workspace.bootstrap_status()` and paints a four-step
   checklist (Provider → Server → Image → VM). Each step turns green when
   `frappe.db.count(<doctype>)` is at least one. When all four are
   satisfied the checklist collapses to a "Bootstrap complete ✓" banner
   and the operator can dismiss it permanently via a per-user default;
   until then `Skip setup` hides it for the current session only.
2. **Fleet at a glance** — four `number_card` blocks: Active Servers,
   Running Virtual Machines, Pending Virtual Machines (tinted amber to
   draw the eye when stuck), Failed Tasks (24h) (tinted red). Frappe's
   Number Card doesn't support threshold-driven colour, so the tint is
   static; visual weight still scales with the count.
3. **Recent activity** — a `quick_list` block bound to Task. The last
   ten Task rows with their status, subject, and relative time, so the
   operator sees what the fleet is doing without leaving the workspace.

The workspace deliberately drops the "Your Shortcuts" row and the
"Reports & Masters" card section that earlier duplicated the sidebar.
The sidebar still carries Home and the five doctype links — that *is*
the right primitive for navigation, so the workspace doesn't repeat it.

The multi-app launcher (`/desk`, `/app/home`) is *not* hidden: Frappe
short-circuits `/desk` rendering before `website_redirects` can fire
([`apps/frappe/frappe/website/path_resolver.py:34`](../../frappe/frappe/website/path_resolver.py#L34)),
so we accept a one-click cost to enter Atlas from a fresh login.
Bookmarks and the sidebar Home button hit `/app/atlas` directly.

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
- A **credential indicator** auto-runs on form refresh for DigitalOcean
  providers. `Server Provider.credential_check` hits the DO `/account`
  endpoint and returns `{ok, email, rate_limit, rate_remaining}` or
  `{ok: false, error}`; the client paints a green
  `✓ API token valid (4999/5000)` or a red `✗ API token invalid` chip
  via `frm.dashboard.add_indicator`. Result is cached for five minutes
  in `frm._atlas_credential_cache`; the **Test Connection** action
  invalidates the cache so the operator can re-verify on demand.

### Server

- **Bootstrap** is primary when the server is `Pending` /
  `Bootstrapping` / `Broken`. On an Active server it folds under
  `Actions ▾` as **Re-bootstrap** — re-bootstrapping a healthy host
  is rare enough not to compete for top-bar real estate.
- **Run Task** and **Reboot** always live under `Actions ▾`.
- **Reboot** is danger. It demands the operator type the server name
  in a `confirm_destructive` dialog that also shows the running-VM
  count.
- A yellow **headline alert** announces any Pending/Running Task on
  this server, linking to the Task form. The alert refreshes on the
  `task_update` realtime event.
- A **Recent Tasks** dashboard section lists the last five Tasks for
  this server with status pill and relative time. Same realtime hook
  refreshes the list.

### Virtual Machine

- Lifecycle buttons follow a status-keyed hierarchy:
  - `Pending` / `Failed` → **Provision** primary.
  - `Stopped` → **Start** primary, **Restart** secondary.
  - `Running` → **Stop** primary, **Restart** secondary.
  - `Terminated` → no lifecycle buttons; instead **Re-provision as
    new** is primary and **Delete record** is danger (under
    `Actions ▾`).
- **Terminate** is always available (until status = Terminated),
  under `Actions ▾`, danger. The `confirm_destructive` dialog shows
  IPv6, image, server, and demands the operator type the VM's 8-char
  short ID.
- The form header carries an `IPv6 [...]` chip — clicking copies
  `ssh root@<ipv6>` to the clipboard. The Networking section
  auto-expands while the VM is `Pending` so the address is visible
  before Provision.
- The Access section renders the same `ssh root@<ipv6>` command in a
  monospace box with a copy button (the IPv6 is the only stable
  identifier outside the desk).
- **Terminated** records render a red dashboard headline
  (`⛔ Terminated <when>. This record is kept for audit; the VM no
  longer exists.`); the **Re-provision as new** button opens a new VM
  form with the same server / image / vcpus / memory / disk / ssh key
  and a `(clone)`-suffixed description pre-filled.
- The list view shows `<description> · <short id>` in the subject
  column, an IPv6 copy chip, and status-coloured indicators
  (`Pending` orange, `Running` green, `Stopped`/`Terminated` grey,
  `Failed` red).
- When the linked provision Task ends in `Failure`, the
  Task.on_update hook flips the VM's `status` from `Pending`/`Running`
  to `Failed` via `frappe.db.set_value` and publishes a
  `virtual_machine_update` realtime event. The VM form subscribes and
  reloads. For `Pending`/`Failed` VMs the client also renders a red
  intro that links to the most recent provision-vm.sh Failure Task —
  the operator clicks the link, reads the error, and clicks Provision
  again to retry.
- The **creation form** (new VM) shows three affordances on top of the
  raw schema: a yellow `Description` nudge until the operator types a
  label; `Small / Medium / Large / Custom` size-preset radios above the
  vCPUs input that write all three Int fields in one click; and a
  dashboard headline `Server capacity: X requested + Y used / Z total
  (N VMs)`. The headline turns orange at the cap and red — with a
  `⚠ Server is oversubscribed` suffix — when projected use exceeds
  total. Capacity is computed by
  `atlas.atlas.api.server_capacity.capacity_for_server`, backed by a
  hand-maintained `size → vCPUs` dict (same maintenance model as the
  monthly-cost dict on Server Provider).

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
- A **Sync Status** table at the top of the form lists each Active
  server with the last successful `sync-image.sh` Task for this image
  (`<when ago>` plus a clickable Task name). Servers never synced show
  **never** and a **Sync now →** shortcut that opens the Sync to
  Server dialog with the server pre-filled.
- Once any successful sync exists for an image, the kernel and rootfs
  fields (`kernel_url`, `kernel_filename`, `kernel_sha256`,
  `rootfs_url`, `rootfs_filename`, `rootfs_sha256`) are **locked**.
  Server-side `validate` throws on any change; the client mirrors the
  lock via `read_only` and shows a blue intro: "This image has been
  synced. To change kernel or rootfs, create a new image
  (e.g. `<name>-v2`)." Editing in place would silently invalidate prior
  audit rows that reference a different digest.

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
- `Task.on_update` propagates status to linked records. For Failure
  with `script = provision-vm.sh` it flips the linked VM's status to
  `Failed` and publishes a `virtual_machine_update` realtime event —
  the VM form re-renders without manual refresh.

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
