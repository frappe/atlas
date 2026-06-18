# User UI — the dashboard SPA

> The operator UI is [10-desk-ui.md](./10-desk-ui.md); this document is its
> counterpart for the *second audience* Atlas now serves — end **users**.

Atlas has two audiences with two UIs:

- **Operators** use **Desk** (`/app/atlas`). They own the fleet: providers,
  servers, image sync, ad-hoc tasks, capacity. Unchanged — see
  [10-desk-ui.md](./10-desk-ui.md).
- **Users** use a **frappe-ui single-page app at `/dashboard`**. They see and
  operate **only their own** Virtual Machines, Snapshots, and SSH Keys, plus
  Images (read-only, shared). They never see Provider, Server, or Task as
  surfaces.

This is a deliberate, documented reversal of the original PoC stance ("Desk is
the UI; no web UI of our own"). The reversal is scoped: Desk stays the
operator UI; the SPA is *additive* for users. Nothing in Desk is removed.

> **Status (Central pivot).** Central ([16-central.md](./16-central.md)) is
> becoming the customer-facing front door for the whole platform. The SPA
> described here is **transitional**: it remains the user surface for this
> iteration but will be **retired** once Central's console fronts the user
> experience. Its permission model (owner-scoped,
> [permissions.py](../atlas/atlas/permissions.py)) is **unchanged for now** —
> Central drives Atlas as a service user and attributes resources via `Tenant`
> ([02-doctypes.md § Tenant](./02-doctypes.md#tenant)) without yet superseding
> owner-scoping.

## Why a second UI (and not more Desk)

Desk is built for an operator reading infrastructure to act on the whole
fleet. A user has a narrower, different job: stand up a machine, reach it,
snapshot it, tear it down — for *their own* machines, with no exposure to
providers, servers, capacity, or the task log. Desk's doctype-per-everything
model can't hide Provider/Server/Task from a user without contorting Desk;
a purpose-built SPA with a three-item world is simpler for the user and keeps
the operator surfaces entirely out of reach.

## The permission split

The SPA introduces Atlas's first multi-tenant boundary. It is enforced at the
**permission layer**, not just hidden in the UI — a user calling the API by
hand is refused.

| DocType                  | Operator (System Manager) | User (`Atlas User`)                         |
| ------------------------ | ------------------------- | ------------------------------------------- |
| Virtual Machine          | all rows, all perms       | **own rows** (`if_owner`): read/write/create/delete |
| Virtual Machine Snapshot | all rows, all perms       | **own rows** (`if_owner`): read/create/delete |
| SSH Key                  | all rows, all perms       | **own rows** (`if_owner`): read/write/create/delete |
| Site                     | all rows, all perms       | **own rows** (`if_owner`): read/write/create/delete |
| Virtual Machine Image    | all rows, all perms       | **read, all rows** (shared base images)     |
| Task                     | all rows (read; no delete)| **read, only Tasks of an owned VM**         |
| Provider / Server        | all rows, all perms       | **no access**                               |
| Provider Size / Image    | all rows                  | **no access**                               |
| Settings (all Singles)   | all                       | **no access**                               |

Mechanics (all in `atlas/atlas/permissions.py`, wired in `hooks.py`):

- **Ownership = Frappe's built-in `owner`.** No owner field is added; Frappe
  stamps `owner` on insert. A user owns the VMs/Snapshots they create.
- **`if_owner: 1`** permission rows on Virtual Machine, Virtual Machine
  Snapshot, SSH Key, and Site for the `Atlas User` role restrict the user to
  their own rows.
- **`permission_query_conditions`** scope list views / `get_list`:
  - Virtual Machine, Virtual Machine Snapshot, SSH Key, Site, Site Request →
    `owner = <user>`.
  - Task → only Tasks whose `virtual_machine` is owned by the user.
  - System Manager → unrestricted (empty condition).

**The one guest-reachable surface.** Almost the entire SPA is login-gated
(`/dashboard` bounces a Guest to `/login`). The exception is the public **signup**
on-ramp ([14-self-serve.md](./14-self-serve.md)): the server-rendered `/signup`
page + the guest API `atlas.atlas.api.signup.request_site`, and the `/verify`
route the emailed link lands on. These are deliberately guest-accessible (a
visitor has no account yet); everything else requires a session. Verification
*creates* the account (a Website User with the `Atlas User` role) and logs them
in, after which the scoping above applies. The user-facing **Sites screen** (list
+ create-from-subdomain, in the SPA) is the remaining plan-04 SPA work; the
permission layer above already scopes it.
- **`has_permission` on Task** guards single-document reads: a user may read a
  Task only if they own its linked VM. Task has no `if_owner` (Tasks are
  stamped with the system user, not the requesting user), so this hook + the
  query condition together produce "own VM's tasks only" — and Task is **never
  a nav item** in the SPA.

The `Atlas User` role ships as a `Role` fixture with `desk_access: 0` — users
live in the SPA, not Desk. Website access is independent of desk access, so an
`Atlas User` can reach `/dashboard` and the standard `frappe.client.*`
endpoints without any Desk footprint.

## What the SPA does not own

- **It defines no new server-side logic.** Every lifecycle action posts to the
  *existing* whitelisted controller methods on the Virtual Machine
  (`provision`, `start`, `stop`, `restart`, `pause`, `resume`, `snapshot`,
  `rebuild`, `resize`, `terminate`). The UI is a client, not a second
  controller.
- **It defines no new API endpoints.** It uses standard Frappe endpoints only:
  `frappe.client.get_list` / `get` (via the frappe-ui `useList` / `useDoc`
  composables), document insert/delete and lifecycle methods (via the
  `useDoctype('Virtual Machine')` composable's `insert` / `delete` /
  `runDocMethod`, which post to the standard `/api/v2/document/...` and
  `run_doc_method` routes — the same contract `frm.call` uses). No bespoke REST
  router, and no hand-built request envelopes.
- **It exposes no *server* placement choice.** A user never picks a server. On
  create they choose the **image** (from the shared, Active Virtual Machine
  Images), and the Virtual Machine controller fills `server` from placement
  (`before_insert`); the operator still controls which servers are Active and
  which images exist. Server placement stays "operator owns the fleet, the
  system slots the VM" — consistent with operating principle #4. "Room" is a
  vCPU budget: a host's physical vCPU total times
  `Atlas Settings.overprovision_factor` (default 1), minus the vCPUs of its
  live VMs. A host whose size has no known vCPU total — an uncatalogued slug or
  a self-managed host with none — counts as unlimited. (Richer availability-
  aware selection is still a later refinement of `default_server`; today it is
  first-Active-with-budget.) When the user omits an image — they can't
  in the dialog, but the controller is called directly in tests — `default_image`
  still applies the operator's configured default.

## Layout & components

The SPA is a Vue 3 + frappe-ui app under `atlas/frontend/`, built to
`atlas/public/frontend`, served via a `www/dashboard.html` page and a
`website_route_rules` entry. It composes frappe-ui components (`Sidebar`,
`Button`, `Badge`, `ListView`, `Dialog`, `FormControl`, `Breadcrumbs`,
`Dropdown`) on the library's semantic tokens (`ink-*` / `surface-*` /
`outline-*`). No hand-rolled markup, no raw palette colors.

**Standard components first — the maintenance bar.** The SPA's review bar is
*"is there a standard frappe-ui component/composable that covers this?"* — and
if there is, we adopt it **even when a hand-rolled version would be shorter.**
The reason is leverage, not line count: every bespoke surface is taste we must
re-tune as the library evolves (spacing, hover/selected states, collapse,
dark-mode, a11y); a standard component inherits those upstream for free. So:

- **App shell** uses the library `Sidebar` / `SidebarHeader` / `SidebarSection`
  / `SidebarItem` — not a hand-rolled `<aside>`. Nav items are a
  `SidebarSection.items` data array (`label` / icon / route `to`); active-state,
  collapse, and collapsed-tooltips come from the component. The user menu
  (Log out) lives in the `SidebarHeader` menu. (Nav icons are passed as small
  icon components rather than `lucide-*` strings — the pinned `SidebarItem`
  renders a string icon as literal text; only the header menu, which routes
  through `Dropdown`, takes the `lucide-*` string directly.)
- **Lifecycle actions** post through the standard **`useDoctype('Virtual
  Machine').runDocMethod` / `.delete`** composable — never a hand-built
  `run_doc_method` envelope or a raw `frappe.client.delete`. (`runDocMethod`
  does not refetch the doc, so the page still reloads the VM + its Tasks after
  each action.) Creation still uses `frappe.client.insert` / `useDoctype.insert`.
- **Confirms** use the library's imperative **`confirmDialog`** (the pinned
  `frappe-ui@0.1.278` API) — not a hand-mounted `<Dialog>` and not a dynamic
  `import('frappe-ui')` (the old code referenced a `dialog.*` namespace that
  doesn't exist in this version, so those confirms were broken). Input-less
  destructive actions (Rebuild, Terminate, Delete) are `confirmDialog` calls;
  the action verb lives in the title since this version's confirm is title +
  message only. **Form** actions keep a real `Dialog` component: Snapshot (name
  the snapshot) and Resize (vCPU / memory / disk) live in `MachineActionDialog`.
  *(A future frappe-ui that ships `dialog.danger` / `dialog.prompt` would let
  Snapshot collapse into a prompt and drop the danger theme by hand — tracked
  as a version-gated follow-up, not done on the pinned version.)*
- **List empty states** are `ListView`'s built-in `options.emptyState` (title /
  description / action button), not a standalone empty-state component. (The
  built-in has no icon slot — a small, accepted visual trade for tracking the
  library.)
- **Relative time** uses the house `dayjs().fromNow()` re-exported by frappe-ui.

A few surfaces are kept hand-rolled **only because the library ships no
equivalent** (confirmed against the library and the CRM/Gameplan apps, which
hand-roll the same): copy-to-clipboard (`CopyText`), the status→theme `Badge`
wrapper (`StatusBadge`), the breadcrumb/title/actions bar (`PageHeader`), and
the `ListView` `#cell` dispatch that renders our `badge` / `copy` / `time` /
`link` cell types (ListView has no built-in cell types for these). These are
the documented exceptions, not licence to hand-roll anything else.

Screens (wireframes in [`ui/wireframes.md`](../ui/wireframes.md)):

1. **App shell** — the library `Sidebar` with four nav items (Machines,
   Images, Snapshots, SSH Keys); the `SidebarHeader` menu = Log out. (The
   header-vs-footer placement of Log out follows the standard `SidebarHeader`
   idiom; a `#footer-items` dropdown is the fallback if a footer is preferred.)
2. **Machines list** — column-aligned rows, status badge, IPv6 copy chip; one
   primary `New Machine` (the header button when populated; `ListView`'s
   built-in empty-state button when empty).
3. **Machine detail** — one status-keyed primary lifecycle action; siblings
   `subtle`; rare/destructive under `Actions ▾`; **the VM's own Tasks shown
   inline** as an Activity list (Tasks have no nav home). Destructive input-less
   actions (Terminate / Rebuild / Delete) are `confirmDialog` confirms; Snapshot
   (name) and Resize (vCPU/memory/disk) are form dialogs (`MachineActionDialog`).
   All lifecycle calls go through `useDoctype('Virtual Machine').runDocMethod`;
   Delete through `.delete`.
4. **New Machine dialog** — four fields (Name, Image, Size preset, SSH key). The
   user picks the base image from the Active shared images and a size from the
   five tiers (Shared 1x/2x/4x/8x, Dedicated 1x; default Shared 1x); the server
   is placed automatically. **SSH key** is a *picker
   over the user's own SSH Keys* (not a raw paste), with an inline "add a new
   key" affordance so a first-time user with no keys is never stuck — the new
   key inserts via the standard `SSH Key` endpoint, then is selected. On create,
   the chosen key's `public_key` body is copied into the VM's immutable
   `ssh_public_key`. Inserts a Virtual Machine via the standard endpoint;
   auto-provision boots it.
5. **Images / Snapshots lists** — read-mostly, same aligned shape.
6. **SSH Keys list** — the user's own keys (name + fingerprint), with an
   **Add SSH key** primary (a `Dialog` naming + pasting the key) and a per-row
   Delete (`confirmDialog`). Same aligned `ResourceList` shape; both mutations
   go through the standard `useDoctype('SSH Key')` `insert` / `delete`.

Design constraints (also the review bar): one primary action per page; color
encodes state only; few words; alignment down every list; consistent spacing;
borders only where they signal something.

## Testing

A user gets a new surface, so a new e2e use-case module
`atlas/tests/e2e/use_cases/user_dashboard.py` proves the bar: a non-operator
`Atlas User`, driving the same standard endpoints the SPA posts to, registers an
`SSH Key`, creates + provisions a VM with it (placement filled, `owner` stamped,
auto-provision boots it), reaches it (IPv6 + that key — the existing
reachability bar), reads its Tasks inline, and is **denied** another user's VM /
SSH Key and all of Provider/Server/global-Task. Unit tests in
`test_permissions.py` (SSH Key `if_owner` scoping) and `test_ssh_key.py` (key
validation + fingerprint) pin the contract in milliseconds.

## Deferred (named, not half-built)

- **Team / sharing model** — slice 1 is strictly per-`owner`. A `Team` doctype
  (Gameplan/CRM style) is a follow-up if multiple users must share a VM.
- **Browser / Playwright e2e** — the bar is proven at the API level as the
  user; pixel-level proof is a follow-up.
- **User-facing image creation, free-form sizes** — users get five size tiers
  (Shared 1x/2x/4x/8x — oversubscribable fractions of a core, 512 MB up — and
  Dedicated 1x, a full core with 8 GB; see [sizes.py](../atlas/atlas/sizes.py))
  and read-only shared images. Building images and free-form (non-preset) sizing
  stay operator-only.
- **SSH key rotation on an existing VM** — a key is immutable on the rootfs
  (`ssh_public_key` is `set_only_once`), so the SSH Keys page adds/removes keys
  from the *account* but does not re-key a running machine. Re-keying a VM is a
  follow-up; today it means terminate + recreate.
- **In-SPA settings** — the header menu is Log out only.
