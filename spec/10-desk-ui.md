# Desk UI

The desk is Atlas's only UI. We don't ship a custom SPA; we lean on
Frappe's standard form, list, and dialog primitives. But every Atlas
form goes through a small layer of shared client conventions so the
operator sees a consistent action hierarchy and can't fire expensive
or destructive things by accident. This section documents what that
layer is and why it exists.

A second, narrower layer — scoped CSS in
[`atlas/public/css/atlas_desk.css`](../atlas/public/css/atlas_desk.css)
loaded via `app_include_css` — closes the visible gap between Atlas
and the Frappe UI / CRM / Gameplan family without touching Desk's
core CSS. Each block is documented at the call site below
(["Visual polish"](#visual-polish)); the source-of-truth audit
(token-level comparison with the Frappe UI apps, plus the list of
drifts each CSS rule addresses) is in
[`ui/audit.md`](../ui/audit.md).

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

#### One primary per page

Desk's own `Save` button (`.standard-actions .primary-action`) is
painted `btn-primary` on every form load, even on a clean record. With
an Atlas lifecycle hero also rendered as `btn-primary`, the page head
ends up with **two solid-black buttons** — breaking the "one primary
button per page" rule from [`llm/Taste.md`](../llm/Taste.md).

[`atlas/public/css/atlas_desk.css`](../atlas/public/css/atlas_desk.css)
fixes this with a scoped `:has()` rule: whenever a custom
`.btn-primary` exists in `.page-actions .custom-actions`, the sibling
`Save` is demoted to a Subtle / Outline variant (white background,
ink-gray-7 text, gray-3 border). Save keeps its click handler and
Ctrl/Cmd+S binding — only the visual weight drops, so the lifecycle
action reads as the page's single hero. On forms with no custom
primary (Active server, idle Task) Save stays solid, correctly
becoming the page's only primary.

### Form-embedded lists — only on the workspace now

The earlier design surfaced three form-embedded `quick_list` panels
(Server > **Recent Tasks**, Task > **Sibling Tasks**, Virtual Machine
Image > **Sync Status**). All three are **gone** — Frappe's Connections
dashboard on Server / VM / Image already exposes the same Task count
and link affordance from the standard `_dashboard.py` config, and
duplicating the navigation inside the form added clutter without
adding signal.

The workspace's **Recent activity** block keeps the `quick_list`
widget (10 most recent Task rows, status pill, relative time) — that's
the operator's home, not a form section, and the at-a-glance affordance
earns its keep there.

### Confirmation helpers

```text
frappe.atlas.confirm_cost({title, body_html, proceed_label, proceed})
frappe.atlas.confirm_destructive({title, body_html, match_string,
                                  match_label, proceed_label, proceed})
```

`confirm_cost` wraps `frappe.warn` with the orange Provision-style
indicator. Used for actions that are not destructive but spend real
money or bandwidth: Provision Server (creates a billable droplet).
There is no Sync-to-All operator action — image sync is automatic on
image insert; see [Virtual Machine Image](#virtual-machine-image).

`confirm_destructive` is a custom dialog with a text-match input. The
red primary button stays disabled until what the operator types
matches `match_string` exactly. Used for: Reboot a server (match the
server `title`), Terminate a VM (match the VM `title`), Delete
a Terminated VM record (match the VM `title`), Archive a Server / Image
(match the row's `title`). The dialog body is empty — typing the title
is the entire deterrent.

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
- `.new-timeline`, `.comment-input-container`, `.comment-input-wrapper`,
  `.comment-input-placeholder`, `.comment-box`, `.comment-box-container`
  inside `frm.page.wrapper` — the activity panel and every known shape
  of the comment box / placeholder Frappe emits across versions.

The main column then expands from `col-lg-8` to `col-lg-12` so the
form breathes. We hide DOM nodes; we don't monkeypatch Frappe globals.

Connections dashboards (the count tiles for Workloads, Tasks, …) stay
visible — those *are* useful and Frappe renders them on the form
itself, not in the right rail.

### Page title

`frappe.atlas.set_window_title(frm)` overrides Frappe's default
`<title> — <name>` (which duplicates the operator-facing label and the
autoname on DocTypes that carry a separate `title` field — Server, VM,
Image). The override sets `document.title` to
`${frm.doc.title || frm.doc.name} — Atlas`. On DocTypes where the
user-defined name *is* the autoname (Server Provider, Task) the
override falls through cleanly to `name`.

## The workspace

The Atlas workspace is the operator's home. It is restructured around three
sections, top-to-bottom:

1. **Bootstrap checklist** — Frappe's native `Module Onboarding` widget,
   wired into the workspace `content` as a `type: "onboarding"` block.
   The four steps (Add Server Provider → Provision Server → Add Virtual
   Machine Image → Provision Virtual Machine) ship as
   [`module_onboarding/atlas_setup/`](../atlas/atlas/module_onboarding/atlas_setup/)
   plus four
   [`onboarding_step/<slug>/`](../atlas/atlas/onboarding_step/)
   JSON files. Each step's `reference_document` points at the target
   DocType; the operator clicks the step, lands on the create form, and
   on save the widget flips `is_complete` for that step. When all four
   are satisfied the widget collapses itself and can be permanently
   dismissed — no Atlas code, no fixture HTML/CSS/JS. The earlier
   custom-HTML implementation (`atlas-bootstrap-checklist`,
   `bootstrap_status()`) is gone. Sites bootstrapped before the
   onboarding fixture landed are migrated by
   [`atlas/patches/v1_0/migrate_workspace_to_onboarding.py`](../atlas/patches/v1_0/migrate_workspace_to_onboarding.py),
   which rewrites the workspace `content` JSON to match
   [`atlas/atlas/workspace/atlas/atlas.json`](../atlas/atlas/workspace/atlas/atlas.json)
   and force-deletes the orphaned `Custom HTML Block atlas-bootstrap-checklist`
   row if it survived. The patch is idempotent — re-running it on a
   clean site is a no-op.
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

## Visual polish

[`atlas/public/css/atlas_desk.css`](../atlas/public/css/atlas_desk.css)
is the *only* CSS Atlas adds to Desk. Every rule below was justified by
a side-by-side comparison with Frappe CRM, Gameplan, and the canonical
Frappe UI components (see [`ui/audit.md`](../ui/audit.md)). The file
is small and scoped — each block opens with a comment that points back
to the audit finding that motivated it.

### Sidebar items — inset and rounded

Desk's stock sidebar items run edge-to-edge with no hover radius. The
Frappe UI `<Sidebar>` (used by CRM and Gameplan) gives every item an
8px horizontal inset and an 8px-radius hover/active fill. Atlas
applies the same shape to `.body-sidebar .standard-sidebar-item`
(and the nested `.sidebar-child-item`). Active items pick up
`--surface-gray-3`; hover lands on `--surface-gray-2`.

Frappe marks the current workspace with `.active-sidebar` (not
`.selected`, which an older spec assumed) — the selector in the CSS
file matches the live DOM. The inner `.item-anchor` is forced
transparent so the radius can clip the fill cleanly.

### Form field labels — softened to ink-gray-5

Desk's `.control-label` defaults to `--ink-gray-7` — only marginally
lighter than the value inside the input, so the eye has to decode
"label" vs "value." Frappe UI's `FormControl` paints labels
`--ink-gray-5`, clearly muted. Atlas applies the same one-line
override (`.frappe-control .control-label { color: var(--ink-gray-5); }`)
so values read louder than their labels. Section headers, modal
titles, and dialog labels are untouched — the rule is scoped to
`.frappe-control`.

### Single-tab forms with collapsible sections

Every Atlas form now collapses to a single `Overview` Tab Break with
the rest of the layout sitting under it as collapsible Section Breaks.
The earlier multi-tab shape (Networking / Host info / Activity / Image
data / Output as siblings of Overview) was scroll-light but
attention-heavy: the operator had to click across tabs to confirm one
fact. Sections under a single tab keep the same vertical density while
letting the operator skim and expand only what matters.

| Doctype | Layout |
| --- | --- |
| Server                | Overview (Identity · Networking · Host info) |
| Virtual Machine       | Overview (Identity · Resources · Networking · Security · Activity) |
| Virtual Machine Image | Overview (Identity · Image data) |
| Task                  | Overview (Status · Variables · Output) |

Dashboard panels (Operations, headlines) render above the tab strip
and remain visible regardless of which section is expanded.

### Tonal dropdown items — red and green

`frappe.atlas.add_danger` already paints destructive Actions-menu rows
with `text-danger` (red text). The CSS now also paints the whole row
`--surface-red-2` on hover, matching the frappe-ui Button
`theme=red, variant=subtle` look. A sibling helper
`frappe.atlas.add_success` does the same in green
(`--surface-green-2` on hover, `--green-800` text) for safe-but-primary
items that fold into Actions on a non-default state (e.g.
`Re-bootstrap` on an Active server).

### List empty-state polish

A filtered list with zero matches rendered top-left aligned with no
breathing room. The CSS centers `.list-view .no-result`, caps it at
420px, gives it 48px of vertical padding, and pushes the "Create a
new …" button below the message. Frappe already ships the icon and
the CTA — Atlas only adjusts the layout, no controller method needed.

### One primary per page — Save demotion

Documented above under [Button-tier convention](#button-tier-convention).
The same CSS file owns the `:has()` rule that demotes Desk's `Save`
to outline whenever an Atlas custom `.btn-primary` exists in the page
head, so the lifecycle action reads as the page's single hero.

### Log panes — taller stdout / stderr on Task

`Task.stdout` and `Task.stderr` are `Code` fields. Desk's default pane
height makes any non-trivial run a scroll-inside-a-textarea exercise.
A scoped CSS rule sets `min-height: 24em` on
`.frappe-control[data-fieldname="stdout"|"stderr"] textarea, .CodeMirror`,
which catches both the plain textarea and the CodeMirror wrapper
(Desk swaps between them depending on the Code field's `options`).
The earlier JS-side `enlarge_log_panes` helper is gone.

## Per-doctype consequences

### Server Provider

- **Provision Server** is the primary action.
- **Test Connection** lives under `Actions ▾`. It's a cheap read-only
  ping; it doesn't need top-bar real estate.
- **Archive** lives under `Actions ▾`, shown only while `is_active = 1`.
  Confirms via `frappe.confirm` (no destructive type-the-title dance —
  archiving is not deletion). The controller's `archive()` flips
  `is_active = 0` via `db.set_value` so the framework's
  `set_only_once` lock is bypassed cleanly.
- The Provision dialog uses standard fieldtype inputs — three editable
  `Select` controls for `region`, `size`, `image` (DigitalOcean), or
  the four networking inputs for Self-Managed. Options for the DO
  selects come from `atlas.atlas.api.provider_options.provider_options`,
  same hand-maintained shape as `DIGITALOCEAN_MONTHLY_COST_USD`. The
  defaults preview HTML block (and the "Provisioning takes ~90s"
  paragraph) is gone — operators read the values straight out of the
  inputs. The dialog still hands off to `confirm_cost` ("Create a
  billable droplet?") before the DO API call.
- A **credential indicator** auto-runs on form refresh for DigitalOcean
  providers. `Server Provider.credential_check` hits the DO `/account`
  endpoint and returns `{ok, email, rate_limit, rate_remaining}` or
  `{ok: false, error}`; the client paints a green
  `✓ API token valid (4999/5000)` or a red `✗ API token invalid` chip
  via `frm.dashboard.add_indicator`. Result is cached for five minutes
  in `frm._atlas_credential_cache`; the **Test Connection** action
  invalidates the cache so the operator can re-verify on demand. Test
  Connection also fires a blue `Testing connection…` toast
  immediately on click so the operator knows the click landed before
  the network round-trip resolves.
- Every Auth + Defaults field paints read-only after first save via
  the framework's `set_only_once` flag. Rotating the API token or the
  SSH key is *not* a form edit — operators replace the file on disk
  (for the SSH key) or create a new Provider row and archive the old
  one (for the token).

### Server

- The Server row's `name` is a UUID; the operator-facing label lives in
  the `title` field. List view, breadcrumbs, and the browser tab title
  all read `title`, not `name`. `set_only_once` freezes `title` and
  `provider` after the first save; the rest of the row is locked once
  written via the controller's `_validate_immutability` (which allows
  `None → value`, so the DigitalOcean provision flow can fill IPv4/6
  after insert).
- **Bootstrap** is primary when the server is `Pending` /
  `Bootstrapping` / `Broken`. On an Active server it folds under
  `Actions ▾` as **Re-bootstrap** — re-bootstrapping a healthy host
  is rare enough not to compete for top-bar real estate.
- **Sync Image** lives under `Actions ▾` on `Active` servers. It opens
  a one-field dialog (a Link to `Virtual Machine Image`, filtered to
  `is_active=1`) and calls `Server.sync_image(image)` — a thin wrapper
  around `Virtual Machine Image.sync_to_server(self.name)`.
- **Archive** lives under `Actions ▾` (hidden once the row is already
  Archived). Confirms via a type-the-title dialog, then sets
  `status = "Archived"`. The row stays in the database; existing FKs
  from Virtual Machine and Task rows continue to work.
- **Reboot** is danger. It demands the operator type the server `title`
  into a `confirm_destructive` dialog; the dialog body is empty
  (no caveat copy) — typing the title is the entire deterrent.
- There is no operator-driven "Run Task" catch-all on the form. The
  `Server.run_task_dialog` controller method is kept for
  `Task.retry`, but the desk surface only exposes scripts that are
  first-class buttons (`Bootstrap`, `Sync Image`, `Reboot`). Lifecycle
  scripts that don't earn a top-level button live on the relevant
  DocType (VM start/stop/restart on the VM form, etc.).
- A yellow **headline alert** announces any Pending/Running Task on
  this server, linking to the Task form. The alert refreshes on the
  `task_update` realtime event.
- The bespoke **Recent Tasks** quick_list has been removed — Frappe's
  Connections dashboard panel (Operations) already exposes the
  Task count and a link to the filtered list.

### Virtual Machine

- Lifecycle buttons follow a status-keyed hierarchy:
  - `Pending` → no primary; `after_insert` already enqueued provision.
    The operator clicks `Save` and the worker takes it from there.
  - `Failed` → **Provision** primary (manual retry after an
    auto-provision failure).
  - `Stopped` → **Start** primary, **Restart** secondary.
  - `Running` → **Stop** primary, **Restart** secondary.
  - `Terminated` → no lifecycle buttons; instead **Re-provision as
    new** is primary and **Delete record** is danger (under
    `Actions ▾`).
- **Terminate** is always available (until status = Terminated),
  under `Actions ▾`, danger. The `confirm_destructive` dialog body is
  empty — typing the VM's `title` into the match field is the entire
  deterrent. IPv6/Image/Server details live in the form behind the
  dialog; the dialog doesn't repeat them.
- Every non-status field paints read-only after first save via the
  controller's `_validate_immutability` (and `set_only_once` on
  `title` / `server` / `ssh_public_key`). The framework's read-only
  hint is mirrored on the client in `refresh` so the lock is visible
  to the operator, not just enforced at save time.
- The form header carries an `IPv6 [...]` indicator chip painted via
  `frm.dashboard.add_indicator` (green when Running, orange when
  Pending, red when Failed, grey otherwise). The Networking section
  auto-expands while the VM is `Pending` so the address is visible
  before Provision.
- The Security section (renamed from Access) carries an `ssh_command`
  field — a `Code` field with `is_virtual: 1` + `read_only: 1`, value
  computed by an `@property ssh_command` on the VM controller
  (`ssh root@<ipv6>`). Frappe's read-only Code control paints its own
  copy button, so we ship no markup of our own. The IPv6 is the only
  stable identifier outside the desk.
- **Terminated** records render a red dashboard headline
  (`⛔ Terminated <when>. This record is kept for audit; the VM no
  longer exists.`); the **Re-provision as new** button opens a new VM
  form with the same server / image / vcpus / memory / disk / ssh key
  and a `(clone)`-suffixed title pre-filled.
- The list view shows `<title> · <short id>` in the subject
  column, an IPv6 copy chip, and status-coloured indicators
  (`Pending` orange, `Running` green, `Stopped`/`Terminated` grey,
  `Failed` red).
- When the linked provision Task ends in `Failure`, the
  Task.on_update hook flips the VM's `status` from `Pending`/`Running`
  to `Failed` via `frappe.db.set_value` and publishes a
  `virtual_machine_update` realtime event. The VM form subscribes and
  reloads. For `Failed` VMs the client also renders a red intro that
  links to the most recent provision-vm.sh Failure Task — the
  operator clicks the link, reads the error, and clicks Provision
  again to retry.
- The **creation form** (new VM) shows two affordances on top of the
  raw schema: a `size_preset` `Select` field (Custom / Small / Medium /
  Large, each labelled with its `vCPU / MB / GB`) at the top of the
  Resources section that writes all three Int fields in one click via
  a one-line `size_preset(frm)` change handler; and — *only* when the
  server is oversubscribed — a red dashboard headline
  `⚠ Server is oversubscribed` with the projected use vs. total. The
  green/blue informational variants of the capacity headline are
  dropped: the operator only needs the warning when something's
  wrong. Capacity is computed by
  `atlas.atlas.api.server_capacity.capacity_for_server`, backed by a
  hand-maintained `size → vCPUs` dict (same maintenance model as the
  monthly-cost dict on Server Provider). The yellow `Description`
  nudge is gone — `reqd: 1` on `title` is the framework's native cue.
  When exactly one Active `Server` exists, the new-VM form's `server`
  field is pre-selected via a 2-row `frappe.db.get_list` lookup in
  `onload`.

### Virtual Machine Image

- The form is **read-only after insert** — there is no primary
  action, no Sync to Server dialog, no Sync to All Servers Actions
  item, no Sync Status panel. Image identity (URLs, checksums,
  filenames, default disk size) is immutable from creation; the
  framework's `set_only_once` paints every field read-only after the
  first save, and the controller's `_validate_immutability` raises on
  any backdoor mutation. Editing in place would silently invalidate
  prior audit rows that reference a different digest.
- **Auto-sync on insert.** `Virtual Machine Image.after_insert`
  enqueues one `sync-image.sh` Task per `Active` Server. The operator
  drops kernel/rootfs URLs + checksums into the form, clicks Save,
  and the fan-out happens automatically. Tracking per-attempt happens
  through the resulting Task rows (filter the Task list by
  `script = sync-image.sh`); the dedicated `Virtual Machine Image
  Sync` DocType scoped in the plan was deferred for the PoC.
- **Archive** lives under `Actions ▾`, shown only while
  `is_active = 1`. Calls the controller's `archive()` method to flip
  `is_active = 0`. Rotating an image is "create a new row, archive the
  old one" — there's no in-place upgrade.
- Ad-hoc per-server sync (e.g. catching up a freshly-Active server)
  goes through the Server form's **Sync Image** Actions item — see
  the Server section above. That dialog calls `Server.sync_image(image)`
  which delegates to `Virtual Machine Image.sync_to_server(self.name)`.

### Task

- The form is read-only (`disable_save()`).
- The list view's Status column renders a coloured pill in its own
  column (driven by the DocType's `states` JSON: `Pending` yellow,
  `Running` blue, `Success` green, `Failure` red). The previous
  Subject-cell-only indicator is gone.
- Status-coloured dashboard headline (the only headline shape on the
  form):
  - Pending → blue, "Queued — waiting for worker."
  - Running → yellow, "Running on <server> — started 12s ago."
  - Success → green, "Completed in 28s. Exit code 0."
  - Failure → red, "Failed in 16s. Exit code 1." The first-stderr-line
    excerpt is gone — the full stderr is one collapsible Output
    section click away.
- **No header chips.** The legacy Server / Virtual Machine /
  Triggered-by chips above the body are removed; those values are
  already fields in the form body and columns in the list view.
- **Retry** button (primary) when status = Failure. Delegates to the
  matching VM controller method (`provision()`, `start()`,
  `terminate()`, …) for VM-scoped scripts, or to
  `Server.run_task_dialog(...)` for server-scoped scripts. The
  state-machine guards live in those methods — the Retry button does
  not duplicate them.
- **No Sibling Tasks panel.** The framework's Connections dashboard
  on the linked Server / Virtual Machine already exposes Task count
  + link; surfacing a second list inside the Task form duplicated
  navigation that's one click away.
- The `Variables (JSON)` field is **pretty-printed for read**: a
  one-shot client formatter parses `frm.doc.variables` on refresh,
  rewrites it with 2-space indent if and only if the parsed value
  round-trips, and refreshes the field without marking the form
  dirty. The stored value is untouched; only the on-screen render
  changes.
- The Output section (stdout + stderr) folds under the Overview tab
  as a collapsible Section Break rather than the old Output tab.
  Routine inspection collapses with one click; debugging expands
  inline without the tab-strip click.
- `Task.on_update` propagates status to linked records. For Failure
  with `script = provision-vm.sh` it flips the linked VM's status to
  `Failed` and publishes a `virtual_machine_update` realtime event —
  the VM form re-renders without manual refresh.

## Why this isn't a custom SPA

Every win above lives in a Frappe `Dialog`, a `Module Onboarding`
widget, a `quick_list` widget, a button group, a form intro, a
dashboard indicator, a `doctype_js` client script, or one small
scoped CSS file. We don't replace the Desk form. We don't add a
route. We don't add a build step. The whole thing is Desk plus
~1.4k lines of shared client JS across the five doctype scripts +
helper module, ~200 lines of scoped CSS
([Visual polish](#visual-polish)), and a handful of whitelisted
controller methods (`provider_options`, `credential_check`,
`archive`, `retry`, `sync_image`, `capacity_for_server`, …).

Anything that *looks* bespoke is borrowed: the workspace onboarding
checklist is Frappe's `Module Onboarding` doctype; the workspace
**Recent activity** block is a `quick_list` widget; the per-script
operator dialogs (Sync Image on Server) are `frappe.ui.Dialog` with
typed fields; the VM size presets are a `Select` field; the VM SSH
command is a virtual `Code` field whose value comes from a
`@property` on the controller; Task list-view status pills come from
the DocType's `states` JSON. The pattern: if Desk has a primitive
for it, we pass parameters to that primitive — we don't hand-roll
markup.

The two places we explicitly fight Desk are documented at the call
site: the chrome strip (right rail + timeline) on every form, and the
Task form's `disable_save()` + dashboard-headline overlay that replaces
the standard read-only field-list affordance with a status-coloured
headline + collapsible Output section. Both are intentional; both are
reversible by removing one client script.
