# User UI — the dashboard SPA

> The operator UI is [10-desk-ui.md](./10-desk-ui.md); this document is its
> counterpart for the *second audience* Atlas now serves — end **users**.

Atlas has two audiences with two UIs:

- **Operators** use **Desk** (`/app/atlas`). They own the fleet: providers,
  servers, image sync, ad-hoc tasks, capacity. Unchanged — see
  [10-desk-ui.md](./10-desk-ui.md).
- **Users** use a **frappe-ui single-page app at `/dashboard`**. They see and
  operate **only their own** Virtual Machines, Images (read-only, shared), and
  Snapshots. They never see Provider, Server, or Task as surfaces.

This is a deliberate, documented reversal of the original PoC stance ("Desk is
the UI; no web UI of our own"). The reversal is scoped: Desk stays the
operator UI; the SPA is *additive* for users. Nothing in Desk is removed.

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
| Virtual Machine Image    | all rows, all perms       | **read, all rows** (shared base images)     |
| Task                     | all rows (read; no delete)| **read, only Tasks of an owned VM**         |
| Provider / Server        | all rows, all perms       | **no access**                               |
| Provider Size / Image    | all rows                  | **no access**                               |
| Settings (all Singles)   | all                       | **no access**                               |

Mechanics (all in `atlas/atlas/permissions.py`, wired in `hooks.py`):

- **Ownership = Frappe's built-in `owner`.** No owner field is added; Frappe
  stamps `owner` on insert. A user owns the VMs/Snapshots they create.
- **`if_owner: 1`** permission rows on Virtual Machine and Virtual Machine
  Snapshot for the `Atlas User` role restrict the user to their own rows.
- **`permission_query_conditions`** scope list views / `get_list`:
  - Virtual Machine, Virtual Machine Snapshot → `owner = <user>`.
  - Task → only Tasks whose `virtual_machine` is owned by the user.
  - System Manager → unrestricted (empty condition).
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
  system slots the VM" — consistent with operating principle #4. (Capacity- and
  availability-aware server selection is a later refinement of `default_server`;
  today it is first-Active-with-room.) When the user omits an image — they can't
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

1. **App shell** — the library `Sidebar` with three nav items (Machines,
   Images, Snapshots); the `SidebarHeader` menu = Log out. (The header-vs-footer
   placement of Log out follows the standard `SidebarHeader` idiom; a
   `#footer-items` dropdown is the fallback if a footer is preferred.)
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
4. **New Machine dialog** — four fields (Name, Image, Size preset, SSH key); the
   user picks the base image from the Active shared images, the server is placed
   automatically; inserts a Virtual Machine via the standard endpoint;
   auto-provision boots it.
5. **Images / Snapshots lists** — read-mostly, same aligned shape.

Design constraints (also the review bar): one primary action per page; color
encodes state only; few words; alignment down every list; consistent spacing;
borders only where they signal something.

## Testing

A user gets a new surface, so a new e2e use-case module
`atlas/tests/e2e/use_cases/user_dashboard.py` proves the bar: a non-operator
`Atlas User`, driving the same standard endpoints the SPA posts to, creates +
provisions a VM (placement filled, `owner` stamped, auto-provision boots it),
reaches it (IPv6 + operator key — the existing reachability bar), reads its
Tasks inline, and is **denied** another user's VM and all of
Provider/Server/global-Task. Unit tests in `test_permissions.py` pin the
permission contract in milliseconds.

## Deferred (named, not half-built)

- **Team / sharing model** — slice 1 is strictly per-`owner`. A `Team` doctype
  (Gameplan/CRM style) is a follow-up if multiple users must share a VM.
- **Browser / Playwright e2e** — the bar is proven at the API level as the
  user; pixel-level proof is a follow-up.
- **User-facing image creation, custom sizes** — users get three size presets
  and read-only shared images; building images and custom sizing stay
  operator-only.
- **In-SPA settings / key rotation** — the footer is Log out only.
