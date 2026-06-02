# Plan — `ui`: a user-facing frappe-ui SPA on an operator-only backend

> Source of truth for this tree until READY. Intake answers live in
> `scratch/active.md` under `## ui`. This plan resolves every open question
> from that block — **there are no open questions below.** Phase 1 is
> wireframes (operator instruction), approved before any backend or wiring.

## Progress (Phases 1–6 implemented; Phase 7 e2e awaits the bench flip)

| Phase | Status | Key artifacts |
| ----- | ------ | ------------- |
| 1 Wireframes | ✅ | `ui/wireframes.md`, `spec/11-user-ui.md` |
| 2 SPA scaffold | ✅ builds clean | `atlas/frontend/` (Vue+frappe-ui), `atlas/atlas/www/dashboard.{html,py}`, `website_route_rules` in `hooks.py`, `test_website_route.py` |
| 3 Permissions | ✅ ruff+JSON clean | `atlas/atlas/permissions.py`, `fixtures/role.json` (Atlas User), `if_owner` rows on VM/Snapshot + read rows on Image/Task, `permission_query_conditions`+`has_permission` in `hooks.py`, rewritten `test_permissions.py` (9 tests) |
| 4 Create+placement | ✅ | `atlas/atlas/placement.py`, `before_insert` wired, `Atlas Settings.default_user_image`, `NewMachineDialog.vue`, `test_placement.py` (5 tests) |
| 5 Lifecycle+activity | ✅ | `data/actions.js` map, `Machine.vue`, `MachineActionDialog.vue`, `ActivityList.vue`, `test_action_map.py` (parser-verified) |
| 6 Polish+spec | ✅ | one-primary-per-page enforced, `ErrorMessage` reused, dead `isGuest` removed, README #47/#26/#45 + principle #1/#4 rewritten, `spec/10` opener fixed |

**Bench-flipped 2026-06-02 — unit suites GREEN (21/21):** `test_permissions`
10, `test_placement` 5, `test_action_map` 3, `test_website_route` 3. `yarn
build` green (frappe-ui 0.1.278). `/dashboard` verified over HTTP: guest → 301
`/login`; authed → 200 SPA shell; hashed JS+CSS → 200.

**Two real bugs found + fixed on the flip:**
1. **Layout depth → 404.** `www/` and the vite build output sat one dir too
   deep. Frappe serves www + `/assets/atlas/…` from `get_app_path("atlas")`
   (the package root, next to `hooks.py`). Moved `www/`→`atlas/www`; vite
   `outDir`→`../public/frontend`; fixed `.gitignore` + `dashboard.py` index path.
2. **Placement perms.** `placement.py` queried `Server`/`Image` as the acting
   `Atlas User` (who can't read `Server`) → "no capacity". Added
   `ignore_permissions=True` (system placement, not user-facing data access).

Main was merged in (the shell→Python script-port commit) — clean auto-merge,
no UI adaptation needed (the SPA drives whitelisted methods, not script names).

**Still TODO — Phase 7 (e2e):** build `atlas/tests/e2e/use_cases/user_dashboard.py`
and run it on a real droplet: a non-operator `Atlas User`, through the SPA's
standard endpoints, creates+provisions a VM (placement fills server/image,
auto-provision boots it), reaches it (IPv6 + key), reads its Tasks inline, and
is denied another user's VM + Provider/Server/global-Task. Then → READY.

## 0. The one sentence

Add a **second audience** to Atlas — end *users*, distinct from the
*operator* — by shipping a small **frappe-ui Vue SPA** at `/dashboard` that
lets a user create + operate **only their own** Virtual Machines, Images and
Snapshots (with each VM's Tasks shown **inline, under the VM**), while
Provider / Server / Task stay invisible **and access-denied**. The SPA calls
**only standard Frappe endpoints** — no new REST routes.

## 1. What we are NOT doing

- **Not** removing or changing the operator's Desk experience. Everything in
  [`spec/10-desk-ui.md`](../../spec/10-desk-ui.md) stays. Desk = operator UI;
  the SPA = user UI. This is purely additive for a new audience.
- **Not** building operator screens in the SPA: provisioning a *Server*,
  configuring a *Provider*, running ad-hoc *Tasks*, syncing images
  fleet-wide — all stay Desk-only.
- **Not** inventing new server-side lifecycle logic. The SPA drives the
  **existing** whitelisted methods on
  [`virtual_machine.py`](../../atlas/atlas/doctype/virtual_machine/virtual_machine.py)
  (`provision` :109, `start` :125, `stop` :141, `restart` :159, `pause` :169,
  `resume` :187, `snapshot` :204, `rebuild` :251, `resize` :302,
  `terminate` :349). UI is a client, not a second controller.
- **Not** creating our own API endpoints / a bespoke REST router. We use the
  standard Frappe method endpoints only: `frappe.client.get_list`,
  `frappe.client.get`, `frappe.client.insert`, and
  `run_doc_method` (what `frm.call` posts to). The one existing whitelisted
  helper precedent is
  [`atlas/api/server_capacity.py:26`](../../atlas/atlas/api/server_capacity.py)
  — we add **zero** new whitelisted functions unless a phase proves one is
  unavoidable, and this plan shows it is not.
- **Not** exposing a server/image *picker* to users. A user does not choose
  *where* their VM runs (operator territory). Placement is filled server-side
  (Phase 4). Users never see `server`.
- **Not** a Team / sharing model in slice 1. Ownership is **per-user**
  (Frappe's built-in `owner`), the cheapest correct model. A `Team` doctype
  is a named roadmap follow-up if sharing is ever required — see §10.
- **Not** Playwright / a headless browser in the test bar. The success proof
  is **API-level, driven as the user** through the same standard endpoints the
  SPA posts to (mirrors [`desk_buttons.py`](../../atlas/tests/e2e/use_cases/desk_buttons.py)).
  A thin manual click-through is the human check; we escalate to a browser
  harness only if the operator asks for pixel proof (deferred, §10).
- **Not** touching the on-host scripts, networking, LVM, jailer — this tree is
  Frappe-side only (perms + SPA). No `scripts/*.sh` change.

## 2. The two halves

The pixels are the visible half; the **permission model is the load-bearing
half**. Today every DocType carries a single `System Manager`-only permission
row (verified: `virtual_machine.json`, `virtual_machine_image.json`,
`virtual_machine_snapshot.json`, `task.json`, `server.json`, `provider.json`),
and `hooks.py:145-150` has `permission_query_conditions` / `has_permission`
**commented out**. `test_permissions.py:1-6` pins the current contract in
prose: *"Atlas is single-role: System Manager reads/writes everything."* This
tree changes that contract, so that test changes with it (Phase 3).

Build order is **perms before pixels** after the wireframe is approved: a SPA
with no ownership boundary would pass a demo and fail reject-bar #2.

---

## 3. Wireframes (Phase 1 deliverable)

Design rules applied throughout (operator's constraints + frappe-ui PATTERNS):
**one primary action per page** · **color only for state** (status badges,
destructive buttons) · **few words** · **column alignment down every list**
(fixed-width right-edge slots) · **consistent spacing** (`px-6` gutters,
`py-3/py-4` header, `space-y-4` stacks, `gap-2` button rows). Components are
frappe-ui (`Button`, `Badge`, `ListView`, `Dialog`, `FormControl`,
`Breadcrumbs`); tokens are `ink-*` / `surface-*` / `outline-*` only.

### 3.1 App shell (`AppShell.vue`)

Sidebar + content. Three nav items only — the user's whole world. **No**
Provider / Server / Task in the nav.

```
┌────────────────┬─────────────────────────────────────────────────────┐
│  Atlas         │                                                       │
│                │   (router-view: the page below)                      │
│  ▸ Machines    │                                                       │
│  ▸ Images      │                                                       │
│  ▸ Snapshots   │                                                       │
│                │                                                       │
│                │                                                       │
│  ───────────   │                                                       │
│  ◔ alice@…  ▾  │                                                       │
└────────────────┴─────────────────────────────────────────────────────┘
```
- Sidebar `w-56 shrink-0`, `border-r border-outline-gray-1`.
- Active item: subtle fill (`bg-surface-gray-2`), 8px inset, 8px radius —
  matches the desk sidebar polish in `spec/10-desk-ui.md` §"Sidebar items".
- Footer = the frappe-ui `Dropdown` on the user's name → only **Log out**.
  (No settings, no team — nothing operator.)

### 3.2 Machines — list (`pages/Machines.vue`)

One primary (`New Machine`, top-right). Every row aligns into columns; the
status badge sits in a fixed-width slot so badges form a clean column.

```
┌───────────────────────────────────────────────────────────────────────┐
│ Machines                                          [ + New Machine ]     │  ← one primary
├───────────────────────────────────────────────────────────────────────┤
│ NAME              STATUS         ADDRESS                  UPDATED        │  ← column header
├───────────────────────────────────────────────────────────────────────┤
│ web-01          ● Running       2606:…:a1f3   ⧉          2h ago         │
│ db-staging      ● Stopped       2606:…:77c2   ⧉          1d ago         │
│ build-box       ◔ Pending       —                        5m ago         │
│ old-worker      ● Terminated    —                        Mar 3          │
└───────────────────────────────────────────────────────────────────────┘
       │              │             │                          │
   title (Link)   Badge w-28    ipv6 + copy chip          modified, w-24
```
- `STATUS` → `<Badge variant="subtle" :theme>` mapped in ONE place:
  `Running→green`, `Stopped→gray`, `Pending→orange`, `Paused→blue`,
  `Failed→red`, `Terminated→gray`. (frappe-ui PATTERNS "Status badges".)
- `ADDRESS` is the only copy affordance (the v6 is the stable identifier,
  per `spec/10-desk-ui.md` Virtual Machine §). Stopped/Pending show `—`.
- Empty state = PATTERNS "Empty state": inbox glyph, "No machines yet",
  "Create one to get started.", the same `New Machine` primary.
- Data via `useList({ doctype: 'Virtual Machine', fields: [...] })` — the
  permission query (Phase 3) already scopes it to the user; **the SPA passes
  no owner filter**, the backend enforces it.

### 3.3 Machine — detail (`pages/Machine.vue`)

The hub. **One** primary = the single most-likely next lifecycle action for
the current status (same status→hero map the Desk uses in
`spec/10-desk-ui.md` Virtual Machine §). Siblings are `subtle`; rare/destructive
fold into an `Actions ▾` `Dropdown`. **Tasks render inline at the bottom — they
have no nav home** (reject bar #4).

```
┌───────────────────────────────────────────────────────────────────────┐
│ Machines / web-01                          [ Stop ]  [ Restart ] [⋯]    │  ← Stop=primary (Running)
│ ● Running                                                               │     Restart=subtle, ⋯=Actions▾
├───────────────────────────────────────────────────────────────────────┤
│  Address     2606:4700:…:a1f3                                  ⧉        │
│  Resources   2 vCPU · 2048 MB · 10 GB                                   │  ← label col w-28, value ink-gray-9
│  Image       ubuntu-24.04-server                                       │
│  SSH         ssh root@2606:4700:…:a1f3                         ⧉        │
├───────────────────────────────────────────────────────────────────────┤
│  Activity                                                               │  ← inline Tasks, NOT a nav item
│  ────────────────────────────────────────────────────────────────────  │
│  ● Success    provision-vm.sh        2h ago                            │
│  ● Success    start-vm.sh            2h ago                            │
│  ◔ Running    stop-vm.sh             just now                          │
└───────────────────────────────────────────────────────────────────────┘
```
- **One primary per page** enforced by a `primaryAction(status)` map →
  exactly one solid `Button`; everything else `subtle` or inside `Actions ▾`.
  - `Running` → primary **Stop**; `Restart`, `Pause` subtle.
  - `Stopped` → primary **Start**; `Restart` subtle; `Snapshot`, `Rebuild`,
    `Resize` under `Actions ▾`.
  - `Paused` → primary **Resume**; `Stop` subtle.
  - `Pending` → no primary (auto-provision is already running); status badge
    only.
  - `Failed` → primary **Provision** (retry).
  - `Terminated` → no lifecycle; `Actions ▾` → **Delete** (danger only).
- **Terminate** lives only under `Actions ▾`, danger, behind `dialog.confirm({
  theme: 'red' })` (PATTERNS "Confirmation flow") — no custom dialog markup.
- Identity block: two-column key/value, **label column fixed width** (`w-28`)
  so values align — the alignment rule made literal. No boxes around it
  (PATTERNS principle 6: a section heading + rows, not a card).
- **Activity** = the VM's own Tasks via
  `useList({ doctype: 'Task', filters: { virtual_machine: name },
  fields: ['name','status','script','creation'], orderBy: 'creation desc',
  limit: 10 })`. Read-only rows: status badge (w-28 slot), script, relative
  time. **No** link to a standalone Task list — there isn't one.

### 3.4 New Machine — dialog (`components/NewMachineDialog.vue`)

A frappe-ui `Dialog` (PATTERNS "Form page" rules: one column, `space-y-4`,
every field a `FormControl`, secondary `Cancel` left + one primary `Create`
right). **No `server` field, no `image` picker** — the user only states
*what* they want, not *where*.

```
┌──────────────────────────── New Machine ─────────────────────────────┐
│                                                                       │
│  Name           [ web-02                                          ]   │
│                                                                       │
│  Size           ( ● Small   ○ Medium   ○ Large )                     │  ← size_preset, presets only
│                  1 vCPU · 512 MB · 4 GB                                │     (no Custom for users)
│                                                                       │
│  SSH key        [ ssh-ed25519 AAAA…                              ]   │
│                                                                       │
│                                            [ Cancel ]  [ Create ]     │  ← one primary
└───────────────────────────────────────────────────────────────────────┘
```
- Three fields only: **Name** (`title`), **Size** (`size_preset`,
  presets only — `Small/Medium/Large`, the labels already on the schema
  Select), **SSH key** (`ssh_public_key`).
- Submit = **standard** `frappe.client.insert` of a `Virtual Machine` doc.
  `after_insert` (`virtual_machine.py:63`) auto-provisions — so the user
  clicks **Create** once and the machine boots itself. On success, route to
  the detail page (already Pending) and `toast.success`.
- `server` + `image` are filled server-side (Phase 4); the insert omits them.

### 3.5 Images & Snapshots (`pages/Images.vue`, `pages/Snapshots.vue`)

Read-mostly lists, same column-aligned shape as Machines. Minimal — these are
secondary to Machines.

```
 Images                                                                     Snapshots
┌────────────────────────────────────────────┐   ┌──────────────────────────────────────────────┐
│ NAME                  DISK         STATUS    │   │ NAME            MACHINE      SIZE      STATUS  │
├────────────────────────────────────────────┤   ├──────────────────────────────────────────────┤
│ ubuntu-24.04-server   4 GB       ● Active    │   │ web-01-may30    web-01      9.8 GB  ● Available│
│ ubuntu-24.04-minimal  4 GB       ● Active    │   │ db-pre-upgrade  db-staging  9.8 GB  ◔ Pending  │
└────────────────────────────────────────────┘   └──────────────────────────────────────────────┘
```
- Snapshots created from the Machine detail (`Actions ▾ → Snapshot`), which
  calls the existing `vm.snapshot(title)` via `run_doc_method`. Each row's
  `MACHINE` links back to its VM detail.
- A snapshot row's only action (on `Available`) is **Restore** / **Clone** /
  **Delete** — but to keep slice 1 minimal these live on the **Machine
  detail's `Actions ▾`** (Rebuild-from-snapshot) and the Snapshot list shows
  **Delete** under a row `Dropdown` only. (Rationale in §6; keeps one mental
  model: you operate a machine from its own page.)
- Images are operator-provisioned; for a user the Images page is **read-only**
  (no New, no buttons) — it exists so the user can see what base images are
  available to rebuild onto.

### 3.6 Login / auth boundary

The SPA route requires a logged-in user holding the **`Atlas User`** role.
Guests get Frappe's standard `/login?redirect-to=/dashboard`. A System Manager
operator *may* open `/dashboard`, but their fleet stays in Desk — the SPA only
ever shows the **owner's** rows (the query condition keys on `owner`, and an
operator who didn't create VMs in the SPA simply sees an empty list there;
they use Desk). No bespoke auth code — Frappe's session + role gate the page
(Phase 2 `www` page guard + Phase 3 role).

---

## 4. The permission model (the load-bearing half)

Resolved decision: **own-by-creator using Frappe's built-in `owner`** — no new
owner field, no Team doctype (slice 1). This is the cheapest correct model and
needs no schema change to VM/Image/Snapshot.

### 4.1 New role: `Atlas User`

- A `Role` JSON fixture at
  `atlas/atlas/role/atlas_user/atlas_user.json` with `desk_access: 0`
  (users live in the SPA, not Desk) — exported via `fixtures` in `hooks.py`
  so a fresh site gets it on migrate.
- Verify `desk_access: 0` is compatible with the `www`-page SPA (it is —
  website access ≠ desk access); confirmed at Phase 3 on the live bench.

### 4.2 DocType permission rows (JSON edits)

Add an `Atlas User` permission row **with `if_owner: 1`** to exactly three
doctypes; leave Provider / Server / Task / Provider Size / Provider Image /
Settings **untouched** (System-Manager-only stays).

| DocType                  | `Atlas User` row                                  |
| ------------------------ | ------------------------------------------------- |
| Virtual Machine          | `read, write, create, delete`, **`if_owner: 1`**  |
| Virtual Machine Snapshot | `read, create, delete`, **`if_owner: 1`**         |
| Virtual Machine Image    | `read` only, **no `if_owner`** (shared, read-only)|

- `if_owner: 1` means a user sees/edits only rows whose `owner` = themselves.
  Frappe stamps `owner` on insert automatically — no field, no code.
- **Image** is the exception: images are operator-built and *shared* (a user
  rebuilds onto `ubuntu-24.04-server`). So `Atlas User` gets **read, all
  rows** — but image rows carry nothing user-private, and `write/create` stay
  off (only the operator, as System Manager in Desk, makes images).

### 4.3 `permission_query_conditions` (list scoping)

Wire `hooks.py:145` (currently commented) to a new
`atlas/atlas/permissions.py`:

```python
# hooks.py
permission_query_conditions = {
    "Virtual Machine":          "atlas.atlas.permissions.owner_only",
    "Virtual Machine Snapshot": "atlas.atlas.permissions.owner_only",
    "Task":                     "atlas.atlas.permissions.task_by_owned_vm",
}
```

```python
# atlas/atlas/permissions.py
import frappe

def owner_only(user=None):
    user = user or frappe.session.user
    if "System Manager" in frappe.get_roles(user):
        return ""                                   # operator: unrestricted
    return f"`tab{...}`.`owner` = {frappe.db.escape(user)}"

def task_by_owned_vm(user=None):
    user = user or frappe.session.user
    if "System Manager" in frappe.get_roles(user):
        return ""
    return f"""`tabTask`.`virtual_machine` in (
        select name from `tabVirtual Machine` where owner = {frappe.db.escape(user)}
    )"""
```

- `owner_only` is parametrized per-doctype via the standard
  `permission_query_conditions` call signature (Frappe passes `user`; the
  doctype name is bound by which key called it — implement two thin wrappers
  or read `frappe.flags`/the closure; finalize the exact mechanics in Phase 3
  against the live framework, the shape above is the contract).

### 4.4 The Task / inline-activity subtlety (reject bars #2 + #4 together)

The idea demands "VM-specific tasks shown along with the VM" **without** giving
users a Task nav home or global Task read. Resolution:

- `Task` gets **NO `Atlas User` DocType permission row** → Task is **not a
  user doctype**; a user cannot list/open Tasks generally, and Task never
  appears in the SPA nav (reject #4 ✓).
- But the **Machine detail's Activity** block needs the VM's own tasks. We do
  **not** add a Task perm row and we do **not** write a custom endpoint.
  Instead: give `Atlas User` a **`read`-only Task row with `if_owner` off**
  BUT gate it through `task_by_owned_vm` query conditions + a `has_permission`
  hook so a user can read **only Tasks linked to a VM they own**:

  ```python
  # hooks.py
  has_permission = { "Task": "atlas.atlas.permissions.task_has_permission" }
  ```
  ```python
  def task_has_permission(doc, user=None, permission_type=None):
      user = user or frappe.session.user
      if "System Manager" in frappe.get_roles(user):
          return True
      if not doc.virtual_machine:
          return False
      return frappe.db.get_value("Virtual Machine", doc.virtual_machine, "owner") == user
  ```

  Net effect: the SPA's `useList({doctype:'Task', filters:{virtual_machine}})`
  (a **standard** `get_list`) returns only that VM's tasks, only if the user
  owns the VM; a user hand-calling `get_list('Task')` with no filter gets
  *their owned VMs' tasks only* (query condition), never the fleet; opening an
  arbitrary Task by name is refused (`has_permission`). Task stays out of the
  nav (no list view affordance in the SPA). **reject #2 ✓ (denied at the
  permission layer, not just hidden) + reject #4 ✓ (inline only).**

  > This `read`-but-scoped Task row is the one perms nuance to *prove* on the
  > live bench (Phase 3): that `if_owner`-off + query-condition + has_permission
  > composes to "own VM's tasks only" and nothing leaks. If Frappe's evaluation
  > order surprises us, fall back to the alternative below.

  **Alternative (only if the above leaks):** keep Task System-Manager-only and
  surface activity through the VM's existing `@frappe.whitelist()` surface —
  but that needs a new method, which the operator's "no new endpoints" rule
  disfavors. The query-condition + has_permission path needs zero new
  endpoints and is the chosen one; the fallback is documented, not built.

### 4.5 Server / image placement without a user picker

`Virtual Machine.server` and `.image` are `reqd: 1`, but a user must not pick a
server. Resolution — fill them **server-side in `before_insert`**, only when
the creator is a non-operator and the fields are blank, using existing data:

- Extend `VirtualMachine.before_insert` (`virtual_machine.py:59`) to call a new
  private helper `_apply_user_defaults()` that, when `not self.server`:
  - picks the **default image** = a single `is_active=1` Virtual Machine Image
    (today there's a deterministic default; if more than one, the operator
    designates one via an `Atlas Settings` `default_user_image` Link — add that
    one Single field, no per-user surface);
  - picks the **server** via the existing capacity notion — reuse the logic
    behind [`server_capacity.capacity_for_server`](../../atlas/atlas/api/server_capacity.py)
    to choose the first `status=Active` server with room. Placement stays
    "operator owns the fleet, the system slots the VM" — no scheduler, matches
    operating principle #4 (one VM per slot, operator picks server → here the
    *default* is auto, the operator still controls which servers are Active).
- This is **controller default logic**, not a new API and not a new lifecycle
  codepath — the SPA still inserts via the standard `frappe.client.insert`.
- Edge: zero Active servers / zero active images → `frappe.throw` a clean
  user-facing message ("No capacity available — contact your operator"),
  surfaced as a `toast.error` in the dialog. Fail loud at the boundary
  (Taste 17).

---

## 5. Where the SPA lives + how it builds (resolved scaffold)

Per `frappe-dev` frontend-vue.md + the frappe-ui skill **SETUP.md** (which
flags the Vite 8 / Tailwind v4 trap explicitly):

```
atlas/
  frontend/                      # NEW — the Vue SPA source
    package.json                 # pins: vite@^5, tailwindcss@^3.4, vue-router@^4, frappe-ui
    vite.config.js               # frappeui({ frontendRoute: '/dashboard', frappeTypes:{...} }) + vue()
    tailwind.config.js           # presets: [frappeUIPreset]  (import 'frappe-ui/tailwind')
    postcss.config.js
    index.html
    src/
      main.js                    # createApp → app.use(router) → app.use(FrappeUI) → mount
      App.vue                    # <FrappeUIProvider><router-view/></FrappeUIProvider>
      router.js                  # /dashboard routes
      style.css                  # @import 'frappe-ui/style.css'; @tailwind base/components/utilities
      AppShell.vue               # sidebar + content (§3.1)
      pages/Machines.vue Machine.vue Images.vue Snapshots.vue Login redirect
      components/NewMachineDialog.vue StatusBadge.vue ActivityList.vue
      composables/               # thin wrappers over useList/useDoc/useCall if needed
  atlas/
    www/dashboard.html           # NEW — jinja host page that boots the SPA bundle
    hooks.py                     # website_route_rules → serve SPA; fixtures → Atlas User role
    public/frontend/             # build output (gitignored or committed per repo norm)
```

- **Version pins are mandatory** (SETUP.md): `tailwindcss@^3.4` (v4 silently
  drops the preset), `vite@^5` (frappe-ui's plugin targets Vite 5),
  `vue-router@^4` (Button injects `Symbol(router)`), plus
  `unplugin-icons`/`@iconify/json`/`lucide-static`. Scaffold by **editing
  these files**, NOT `npm create vite`.
- Route: `website_route_rules = [{"from_route": "/dashboard/<path:app_path>",
  "to_route": "dashboard"}]` + a `www/dashboard.html` that includes the built
  `index.html` assets. `frontendRoute: '/dashboard'` in `vite.config.js`.
- Build: `yarn build` → `atlas/public/frontend`; `bench build --app atlas`
  wraps it. Add the build to the repo's CI/pre-commit story (Phase 5).
- **Dark-mode check** (PATTERNS): semantic tokens only ⇒ toggle
  `[data-theme="dark"]` renders clean; verify before declaring a page done.

---

## 6. Phases (small, testable, verifiable; verify after each)

> Per WORKFLOW: one phase at a time, verify, fix before proceeding. The slow
> on-host e2e is **one batched bench flip** near the end (Phase 7), not
> serialized per phase — Phases 1–6 are unit/static/local-dev verifiable.

### Phase 1 — Wireframes & design doc *(operator gate — STOP for thumbs-up)*
- Deliverable: this plan's §3 wireframes promoted into
  `spec/11-user-ui.md` (draft) + a short `ui/wireframes.md` with the ASCII
  frames and the component/token mapping. No code.
- Verify: operator reads the frames, confirms layout + visible/hidden split +
  the "one primary per page" and alignment treatment. **Gate before Phase 2.**

### Phase 2 — SPA scaffold (renders, empty data)
- Build the `frontend/` per §5 with the pinned versions; AppShell + three empty
  pages + router + `www/dashboard.html` + `website_route_rules`.
- Wire `useList` for Machines/Images/Snapshots (will be empty until Phase 3
  grants a user read).
- Verify (local, no host): `yarn dev` renders the shell with Inter font +
  semantic surfaces; DevTools console clean (the SETUP.md sanity checklist);
  `/dashboard` serves the built bundle under `bench start`. **Unit:** a tiny
  `test_website_route.py` asserts the route rule resolves.

### Phase 3 — Permission model
- Add `Atlas User` role JSON + `fixtures` entry in `hooks.py`.
- Add the `Atlas User` permission rows to the three DocType JSONs (§4.2).
- Create `atlas/atlas/permissions.py` (`owner_only`, `task_by_owned_vm`,
  `task_has_permission`) and wire `permission_query_conditions` +
  `has_permission` in `hooks.py` (§4.3, §4.4).
- **Rewrite [`test_permissions.py`](../../atlas/tests/test_permissions.py)** —
  its docstring's "single-role" claim is now false. Add the new contract as
  unit tests (run in ms, no host):
  - an `Atlas User` reads **own** VM/Snapshot, is **denied** another user's;
  - is **denied** Provider/Server read entirely;
  - reads **own VM's** Tasks, **denied** an unrelated VM's Task and the global
    Task list (the §4.4 nuance — this is the test that guards reject #2/#4);
  - reads Images (shared) but cannot create/write them;
  - System Manager still reads everything (operator unaffected).
- Verify: `bench --site atlas.tests.local run-tests --app atlas` green;
  the SPA lists now populate for a logged-in `Atlas User`.

### Phase 4 — Create + placement
- Extend `VirtualMachine.before_insert` → `_apply_user_defaults()` (§4.5);
  add the `Atlas Settings.default_user_image` Single field if needed.
- Build `NewMachineDialog.vue`; submit via standard `frappe.client.insert`;
  route to detail on success; `toast` the throw on no-capacity.
- Verify: **unit** `test_user_defaults.py` — a VM inserted by an `Atlas User`
  with no `server`/`image` gets them filled from defaults + stamps `owner`;
  zero-capacity throws the clean message. (No host — pure controller logic.)

### Phase 5 — Lifecycle actions + inline activity
- Machine detail: the `primaryAction(status)` map → one primary, siblings
  `subtle`, rare/destructive under `Actions ▾`; each action posts through
  `run_doc_method` to the **existing** whitelisted method; `dialog.confirm`
  for destructive; `toast` + re-fetch on return.
- Activity list = `useList` Task scoped by `virtual_machine` (§3.3/§4.4).
- Snapshot/Rebuild/Resize dialogs (frappe-ui `Dialog`, PATTERNS form rules).
- Verify: **unit** that the JS action→method map matches the controller's
  whitelisted set (a `test_action_map.py` reflecting on `virtual_machine.py`
  `@frappe.whitelist` methods, mirroring the spirit of
  `test_scripts_catalog.py`); local dev click-through of each button against a
  Stopped/Running fixture.

### Phase 6 — Polish & reduce (the "clean on arrival" pass)
- Run the §3 design rules as a checklist: one primary per page (grep the SPA
  for `variant="solid"` — at most one per page component); color only on
  badges/danger; label columns fixed-width; consistent `px-6`/`py-*`/`gap-2`;
  dark-mode toggle clean.
- **Reuse pass** (WORKFLOW step 4 / prompt 31): every list is `ListView`,
  every overlay `Dialog`/`dialog.confirm`, every input `FormControl`, every
  API call `useList`/`useDoc`/`useCall` — zero hand-rolled markup, zero raw
  palette colors (reject #3). Delete any composable that just wraps one call.
- Update `spec/11-user-ui.md` to final; rewrite `spec/README.md` non-goal #47
  and operating principle #1 (§8).

### Phase 7 — e2e proof *(batched bench flip — turn-taking)*
- New use-case module
  `atlas/tests/e2e/use_cases/user_dashboard.py` (a user gets a new surface ⇒ a
  new module, per `spec/README.md` testing rules), mirroring
  `desk_buttons.py`'s `run_doc_method` driver but **`frappe.set_user(<Atlas
  User>)`** as the actor:
  1. As an `Atlas User`, `frappe.client.insert` a VM (no server/image) →
     placement fills them, `owner` stamped, auto-provision boots it →
     `wait_for_vm_running` → guest reachable (IPv6 + operator key) — the
     existing reachability bar, but **created and driven as the user**.
  2. The user reads **own** VM + its Tasks inline; a **second** `Atlas User`
     is **denied** the first's VM (perm throw) and sees an empty list.
  3. The user is **denied** Provider/Server/Task-global reads (assert the
     throws) — the leak guard (reject #2).
- Add `user_dashboard` to the operator-use-case table framing in
  `spec/README.md` (the SPA adds a *user* audience; note the user/operator
  split there).
- Verify: **STOP** — "ready to verify — `atlas-tree ui` when free." Operator
  flips live; `bench --site atlas.tests.local execute
  atlas.tests.e2e.use_cases.user_dashboard.run_smoke` (host facts) +
  `run-tests --app atlas` (units). Fix on real-bench findings, re-flip.

### Phase 8 — READY
- Tests pass (units + the one e2e on a real droplet) · UI minimized & reused
  · spec rewritten (README #47 + principle #1 + new `spec/11-user-ui.md`) ·
  `llm/state/` near-empty. Mark `ui` READY in `active.md`; await verdict.

---

## 7. Reject-bar trace (how each phase clears the four bars)

1. **Spec rewritten honestly** → Phase 1 drafts + Phase 6/8 finalizes
   `spec/11-user-ui.md`; README #47 + principle #1 rewritten as a documented
   reversal (Desk=operator, SPA=user). §8.
2. **No operator-surface leak** → Phase 3 perms: Provider/Server have no
   `Atlas User` row (denied); Task is scoped by ownership via query-condition
   + `has_permission` (denied at the layer, §4.4); SPA nav omits all three.
   Tested in the rewritten `test_permissions.py` + e2e step 3.
3. **frappe-ui, not hand-rolled** → Phases 2/5 build only with frappe-ui
   components + semantic tokens; Phase 6 greps for raw `<button>`/palette
   colors and the one-primary rule. The SETUP/PATTERNS rules are the bar.
4. **Per-VM tasks co-located** → §3.3 Activity is inline-only; Task has no SPA
   nav/list; §4.4 perms make a global Task list impossible for a user anyway.

## 8. Spec changes (the honest reversal)

- `spec/README.md:47` non-goal "No web UI of our own. Desk is the UI." →
  rewritten: "Desk is the **operator** UI. A separate **frappe-ui SPA at
  `/dashboard`** is the **user** UI (see [11-user-ui.md]); it exposes only
  Virtual Machine / Image / Snapshot, scoped per-owner." Keep still-deferred
  items (Team/sharing, browser e2e) honestly listed.
- `spec/README.md` operating principle #1 "Desk is the UI … No custom pages."
  → split into the two-audience statement; the SPA is the documented
  exception, the way `hardening`/jailer documented "root everywhere" reversals.
- New `spec/11-user-ui.md`: route, the user/operator permission split table
  (§4.2), the inline-task rule, the placement-default rule, the wireframes,
  and the deferred tail.
- `spec/README.md` "Read this in order" + use-case table: add the SPA / the
  `user_dashboard` e2e module.

## 9. Risks (named, with the mitigation already chosen)

1. **Perm composition for inline Tasks (§4.4)** — the read-but-scoped Task row
   is the one subtlety; mitigation: unit + e2e assert no leak; documented
   fallback (whitelisted method) if Frappe's eval order surprises us.
2. **`desk_access: 0` vs `www` SPA** — confirm an `Atlas User` with no desk
   access can still hit `/dashboard` and the standard `frappe.client.*`
   endpoints (they can; website ≠ desk). Verified Phase 3 on bench.
3. **Build trap** — Vite 8 / Tailwind v4 silently break frappe-ui; mitigation:
   pin per SETUP.md, run the sanity checklist in Phase 2.
4. **Placement default when >1 active image** — resolved by the optional
   `Atlas Settings.default_user_image`; if unset and exactly one active image
   exists, use it; else throw a clean message.
5. **Two-audience spec drift** — the README rewrite is a reject bar; Phase 6/8
   gate it.

## 10. Deferred (named, not half-built) — for `spec/11-user-ui.md` + `09-roadmap.md`

- **Team / sharing model** (multi-user-owns-a-VM) — slice 1 is per-`owner`.
- **Browser/Playwright e2e** — slice 1 proves the bar at the API level as the
  user; pixel-level proof is a follow-up if the operator wants it.
- **User-facing Image creation / a size picker beyond presets / custom sizes**
  — users get the three presets; Custom stays operator-only.
- **In-SPA settings / profile / key management** — the footer dropdown is
  Log out only; key rotation stays out of slice 1.

---

### Single-bench rule reminder
All Phase 1–6 verification is unit/static/local-dev (seconds, no host). Only
Phase 7's `user_dashboard` e2e needs the tree live. Write everything, then
**stop and say "ready to verify — `atlas-tree ui` when free."** Treat the flip
as a turn-taking checkpoint.
