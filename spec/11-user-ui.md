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
  `frappe.client.get_list` / `get` / `insert` (via the frappe-ui `useList` /
  `useDoc` composables) and `run_doc_method` (what `frm.call` posts to). No
  bespoke REST router.
- **It exposes no placement choice.** A user never picks a server. On create,
  the Virtual Machine controller fills `server` and `image` from defaults
  (`before_insert`); the operator still controls which servers are Active and
  which image is the default. Placement stays "operator owns the fleet, the
  system slots the VM" — consistent with operating principle #4.

## Layout & components

The SPA is a Vue 3 + frappe-ui app under `atlas/frontend/`, built to
`atlas/public/frontend`, served via a `www/dashboard.html` page and a
`website_route_rules` entry. It composes frappe-ui components (`Button`,
`Badge`, `ListView`, `Dialog`, `FormControl`, `Breadcrumbs`, `Dropdown`) on
the library's semantic tokens (`ink-*` / `surface-*` / `outline-*`). No
hand-rolled markup, no raw palette colors.

Screens (wireframes in [`ui/wireframes.md`](../ui/wireframes.md)):

1. **App shell** — sidebar with three nav items (Machines, Images, Snapshots);
   footer dropdown = Log out.
2. **Machines list** — column-aligned rows, status badge, IPv6 copy chip; one
   primary `New Machine`.
3. **Machine detail** — one status-keyed primary lifecycle action; siblings
   `subtle`; rare/destructive under `Actions ▾`; **the VM's own Tasks shown
   inline** as an Activity list (Tasks have no nav home).
4. **New Machine dialog** — three fields (Name, Size preset, SSH key); inserts
   a Virtual Machine via the standard endpoint; auto-provision boots it.
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
