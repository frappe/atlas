# Workspace — solution

Maps to [research/01-workspace.md](../research/01-workspace.md).

## 1. No empty-state / no zero-to-one guidance

### Problem
A fresh operator landing on the Atlas workspace has no idea of the
required order (Provider → Server → Image → VM). Shortcuts point
everywhere with equal weight.

### Solution

Drive the workspace from operator state. The workspace block JSON
already supports the **Onboarding** card type and step ordering — but a
simpler win is a top-of-page **HTML block** that renders a four-step
checklist whose checks call `frappe.db.count(...)`. Each row turns green
when at least one record of that type exists.

Server-side: add `atlas.api.workspace.bootstrap_status()` that returns
the four counts in one call. The workspace HTML block uses
`frappe.call("atlas.api.workspace.bootstrap_status")` and renders the
checklist.

The list of steps mirrors `spec/README.md` "First run on a fresh site":

1. Create a Server Provider
2. Provision your first Server
3. Add a Virtual Machine Image
4. Provision your first Virtual Machine

Once all four conditions are met, hide the checklist entirely (it
collapses to a single `Bootstrap complete ✓` chip the operator can
dismiss permanently via a per-user `frappe.boot.user.defaults` flag).

### Wireframe

```
┌────────────────────────────────────────────────────────────────────────┐
│ ⌂ / Atlas                                                       •••   │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │  Get Atlas running                                  [Skip setup] │ │
│  │                                                                  │ │
│  │  ✓  1. Add a Server Provider          [bootstrap-provider →]    │ │
│  │  ✓  2. Provision a Server             [bootstrap-server-… →]    │ │
│  │  ○  3. Add a Virtual Machine Image    [+ Add Image]              │ │
│  │  ○  4. Provision a Virtual Machine    (locked until step 3)      │ │
│  │                                                                  │ │
│  │  Or run `bench --site … execute atlas.bootstrap.run` to do all   │ │
│  │  four in one shot.                                               │ │
│  └──────────────────────────────────────────────────────────────────┘ │
│                                                                        │
│  Fleet at a glance      (see §3 below)                                │
│  ...                                                                  │
└────────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- Workspace **HTML block** (already supported in
  `workspace.content`) calling a whitelisted Python method.
- `frappe.call("atlas.api.workspace.bootstrap_status")` → returns
  `{providers: int, servers: int, images: int, virtual_machines: int}`.
- Per-step CTA buttons use `frappe.set_route("Form", "Server Provider", "new-…")`
  for unsatisfied steps; satisfied steps link to the most recent record.

### Fighting Desk?
No. Workspace HTML blocks are a standard primitive.

---

## 2. Atlas opens in a new tab

### Problem
Clicking the Atlas app icon on `/desk` opens the workspace in a new
window, splitting the session. Confusing.

### Solution

The home grid (`/desk`) is Frappe's "app launcher" workspace and the app
icon's `link_to: app/atlas` opens in a new browser tab by default. Two
remedies, in increasing order of invasiveness:

1. **Remove the app launcher entirely** for the Atlas site. Atlas is a
   single-app site; the launcher only makes sense for multi-app benches.
   Override `frappe.boot.home_page` via a `boot_session` hook in
   `hooks.py` to send users straight to `/app/atlas` instead of
   `/app/home`.
2. **If the launcher must stay**, change the icon's link to a
   `same-tab` route. The launcher icon is rendered from `frappe.boot.allowed_workspaces`;
   we can override the `_target` attribute in a small client script that
   removes `target="_blank"` from the Atlas icon. Hacky but harmless.

Pick option 1. The launcher serves no purpose here.

### Wireframe

```
Login → /app/atlas    (single redirect, no app grid)
```

### Frappe components used
- `hooks.py` → `boot_session = ["atlas.boot.set_home_page"]`
- `frappe.local.session.data.home_page = "/app/atlas"` in
  `set_home_page`.

### Fighting Desk?
**Mild.** Setting the home page is supported but the app launcher
behavior is baked in. Option 1 sidesteps the launcher entirely; we don't
have to fight it.

---

## 3. Workspace is a generic Frappe workspace, not an Atlas dashboard

### Problem
"Active Servers: 1", "Running VMs: 0" are inert numbers. No fleet
overview, no recent activity, no callout for stuck VMs.

### Solution

Restructure the workspace `content` JSON into three meaningful sections:

1. **Bootstrap checklist** (from §1, hides itself when complete).
2. **Fleet at a glance** — four `number_card` blocks with **filter
   links** wired in:
   - Active Servers → `Server` list filtered `status = Active`.
   - Running VMs → `Virtual Machine` list filtered `status = Running`.
   - Pending VMs (stuck) → `Virtual Machine` list filtered
     `status = Pending`. **Highlighted yellow if > 0.**
   - Failed Tasks (24h) → `Task` list filtered
     `status = Failure, modified > now - 24h`. **Red if > 0.**
3. **Recent activity** — a `quick_list` block bound to `Task` with
   `sort = modified desc, limit = 10`. Shows the last 10 tasks across
   the fleet with their status pill, script name, server, and
   relative time.

`number_card` already supports a `color` field driven from
`document_type` + `function` + `filters_json`. Frappe's Number Card
doctype lets you set color rules — for the "stuck Pending VMs" and
"Failed Tasks" cards we set `color = "Red"` and use a simple boolean
threshold (color shown only when count > 0).

### Wireframe

```
┌────────────────────────────────────────────────────────────────────────┐
│ ⌂ / Atlas                                                       •••   │
├────────────────────────────────────────────────────────────────────────┤
│  (bootstrap checklist — shown only when incomplete; §1)               │
│                                                                        │
│  Fleet at a glance                                                    │
│  ┌──────────┬──────────┬──────────────┬───────────────┐               │
│  │ Servers  │ Running  │ Pending VMs  │ Failed Tasks  │               │
│  │   1      │   VMs    │      3       │     (24h)     │               │
│  │ Active   │   0      │    (stuck)   │       1       │               │
│  └──────────┴──────────┴──────────────┴───────────────┘               │
│                            ^^ yellow         ^^ red                   │
│                                                                        │
│  Recent activity                                          View all →  │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │ ● Failure  provision-vm.sh   bootstrap-server-…    17 min ago   │ │
│  │ ● Success  provision-vm.sh   bootstrap-server-…    35 min ago   │ │
│  │ ● Success  terminate-vm.sh   bootstrap-server-…    36 min ago   │ │
│  │ …                                                                │ │
│  └──────────────────────────────────────────────────────────────────┘ │
│                                                                        │
│  Browse  Server Provider │ Server │ Virtual Machine │ Image │ Task    │
└────────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- `number_card` blocks with `color` set per threshold via Number Card
  doctype rules.
- `quick_list` block bound to `Task` for the activity feed.
- A small HTML block for the section labels (already used today).

### Fighting Desk?
No. Workspaces are content-driven JSON and this is exactly the use case
they're built for.

---

## 4. Shortcut cards duplicate the sidebar

### Problem
`Server`, `Virtual Machine`, `Task`, `Virtual Machine Image` shortcuts
all live in the left sidebar already.

### Solution

Drop the "Your Shortcuts" header and the four `shortcut` blocks from
the workspace `content`. Promote the sidebar's existing links — they
are already the right primitive for navigation. The top of the workspace
becomes the bootstrap checklist (when incomplete) and the fleet glance
(always).

Keep the **Browse** strip at the bottom of the workspace as a
discoverable jump for new users; sidebar links are still the canonical
nav.

### Wireframe
See §3 — the shortcut row is gone; "Browse" lives at the bottom.

### Frappe components used
- Edit `workspace/atlas/atlas.json` → remove `sc_server`, `sc_vm`,
  `sc_task`, `sc_image` blocks from `content` and the matching
  `shortcuts` entries (the sidebar links live in `links`, which we
  keep).

### Fighting Desk?
No.
