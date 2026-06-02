# User SPA — wireframes

The user-facing dashboard at `/dashboard`. ASCII frames + the component /
token mapping that makes each screen read as a Frappe screen. This is the
design source for [`spec/11-user-ui.md`](../spec/11-user-ui.md); the plan that
produced it is [`llm/state/plan.md`](../llm/state/plan.md) §3.

## Design rules (applied on every screen)

1. **One primary action per page.** Exactly one solid
   `Button variant="solid" theme="gray"`. Everything else is `subtle` or
   folds into `Actions ▾`.
2. **Color encodes state only.** Status `Badge`s and the destructive button
   carry color. Cards, headings, icons, section rules stay ink-gray /
   surface-* — never themed for decoration.
3. **Few words.** Three nav items. Terse labels. No helper paragraphs where a
   label suffices.
4. **Alignment down every list.** Repeating right-edge elements (badge,
   timestamp, copy chip) get a fixed-width slot (`w-28 shrink-0`,
   `w-24 shrink-0`) so they form a column, not a ragged flex edge.
5. **Consistent spacing.** `px-6` page gutters · `py-3`/`py-4` headers ·
   `space-y-4` form stacks · `gap-2` button rows · `w-28` label column.
6. **Borders earn their place.** Section heading + `divide-y
   divide-outline-gray-1` rows, not a card around everything. Boxes only for
   dialogs, popovers, and genuinely interactive surfaces.

Tokens: `ink-*` / `surface-*` / `outline-*` only — no raw Tailwind palette.
Components: `Button`, `Badge`, `ListView`, `Dialog`, `FormControl`,
`Breadcrumbs`, `Dropdown` (frappe-ui). Icons: `lucide-*` CSS classes.

## Status → Badge theme (defined once)

| Status      | Theme  |
| ----------- | ------ |
| Running     | green  |
| Stopped     | gray   |
| Pending     | orange |
| Paused      | blue   |
| Failed      | red    |
| Terminated  | gray   |

Snapshot/Image: `Available`/`Active` → green, `Pending` → orange,
`Failed` → red.

---

## 1. App shell — `AppShell.vue`

Sidebar (`w-56 shrink-0`, `border-r border-outline-gray-1`) + content. Three
nav items — the user's whole world. **No Provider / Server / Task.**

```
┌────────────────┬─────────────────────────────────────────────────────┐
│  Atlas         │                                                       │
│                │   (router-view: the page below)                       │
│  ▸ Machines    │                                                       │
│  ▸ Images      │                                                       │
│  ▸ Snapshots   │                                                       │
│                │                                                       │
│                │                                                       │
│  ───────────   │                                                       │
│  ◔ alice@…  ▾  │                                                       │
└────────────────┴─────────────────────────────────────────────────────┘
```

- Active nav item: `bg-surface-gray-2`, 8px inset, 8px radius (matches the
  desk sidebar polish in `spec/10-desk-ui.md`).
- Footer = `Dropdown` on the user's name → **Log out** only. No settings, no
  team, nothing operator.

## 2. Machines — list (`pages/Machines.vue`)

One primary (`New Machine`, top-right). Rows align into columns.

```
┌───────────────────────────────────────────────────────────────────────┐
│ Machines                                          [ + New Machine ]     │  ← one primary
├───────────────────────────────────────────────────────────────────────┤
│ NAME              STATUS         ADDRESS                  UPDATED        │
├───────────────────────────────────────────────────────────────────────┤
│ web-01          ● Running       2606:…:a1f3   ⧉          2h ago         │
│ db-staging      ● Stopped       2606:…:77c2   ⧉          1d ago         │
│ build-box       ◔ Pending       —                        5m ago         │
│ old-worker      ● Terminated    —                        Mar 3          │
└───────────────────────────────────────────────────────────────────────┘
      title          Badge w-28    ipv6 + copy chip       modified, w-24
```

- `STATUS` → `Badge variant="subtle"` themed by the table above.
- `ADDRESS` is the only copy affordance (the IPv6 is the stable identifier).
  Stopped/Pending show `—`.
- Empty state: inbox glyph, "No machines yet", "Create one to get started.",
  the same `New Machine` primary.
- Data: `useList({ doctype: 'Virtual Machine', fields: [...] })`. The SPA
  passes **no owner filter** — the backend permission query scopes it.

## 3. Machine — detail (`pages/Machine.vue`)

The hub. One status-keyed primary; siblings `subtle`; rare/destructive under
`Actions ▾`. **Tasks render inline at the bottom — no nav home.**

```
┌───────────────────────────────────────────────────────────────────────┐
│ Machines / web-01                          [ Stop ]  [ Restart ] [⋯]    │  ← Stop=primary (Running)
│ ● Running                                                               │     Restart=subtle, ⋯=Actions▾
├───────────────────────────────────────────────────────────────────────┤
│  Address     2606:4700:…:a1f3                                  ⧉        │
│  Resources   2 vCPU · 2048 MB · 10 GB                                   │  ← label col w-28
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

Primary-action map (`primaryAction(status)` → exactly one solid button):

| Status      | Primary    | Subtle siblings   | Actions ▾                         |
| ----------- | ---------- | ----------------- | --------------------------------- |
| Running     | Stop       | Restart, Pause    | Terminate (danger)                |
| Stopped     | Start      | Restart           | Snapshot, Rebuild, Resize, Terminate (danger) |
| Paused      | Resume     | Stop              | Terminate (danger)                |
| Pending     | — (none)   | —                 | —                                 |
| Failed      | Provision  | —                 | Terminate (danger)                |
| Terminated  | — (none)   | —                 | Delete (danger)                   |

- Identity block: two-column key/value, label column `w-28` so values align.
  No box (heading + rows).
- Destructive actions go through `dialog.confirm({ theme: 'red' })` — no
  custom dialog markup.
- **Activity** = the VM's own Tasks:
  `useList({ doctype: 'Task', filters: { virtual_machine: name },
  fields: ['name','status','script','creation'], orderBy: 'creation desc',
  limit: 10 })`. Read-only rows: status badge (`w-28` slot), script, relative
  time. No link to a standalone Task list — there isn't one.

## 4. New Machine — dialog (`components/NewMachineDialog.vue`)

`Dialog` with the form-page rules: one column, `space-y-4`, every field a
`FormControl`, secondary `Cancel` left + one primary `Create` right. **No
`server` field, no `image` picker.**

```
┌──────────────────────────── New Machine ─────────────────────────────┐
│                                                                       │
│  Name           [ web-02                                          ]   │
│                                                                       │
│  Size           ( ● Small   ○ Medium   ○ Large )                     │  ← size_preset, presets only
│                  1 vCPU · 512 MB · 4 GB                                │
│                                                                       │
│  SSH key        [ ssh-ed25519 AAAA…                              ]   │
│                                                                       │
│                                            [ Cancel ]  [ Create ]     │  ← one primary
└───────────────────────────────────────────────────────────────────────┘
```

- Three fields: **Name** (`title`), **Size** (`size_preset` — `Small / Medium
  / Large`, the labels already on the schema Select; no `Custom` for users),
  **SSH key** (`ssh_public_key`).
- Submit = standard `frappe.client.insert` of a `Virtual Machine`.
  `after_insert` auto-provisions; the user clicks **Create** once and the
  machine boots itself. Route to detail on success; `toast.success`.
- `server` + `image` are filled server-side (`before_insert`); the insert
  omits them. No-capacity → a clean `toast.error`.

## 5. Images & Snapshots (`pages/Images.vue`, `pages/Snapshots.vue`)

Read-mostly lists, same column-aligned shape. Minimal — secondary to
Machines.

```
 Images                                                Snapshots
┌──────────────────────────────────────────┐  ┌──────────────────────────────────────────────┐
│ NAME                  DISK      STATUS     │  │ NAME            MACHINE     SIZE      STATUS    │
├──────────────────────────────────────────┤  ├──────────────────────────────────────────────┤
│ ubuntu-24.04-server   4 GB    ● Active     │  │ web-01-may30    web-01     9.8 GB ● Available  │
│ ubuntu-24.04-minimal  4 GB    ● Active     │  │ db-pre-upgrade  db-staging 9.8 GB ◔ Pending    │
└──────────────────────────────────────────┘  └──────────────────────────────────────────────┘
```

- **Images** are operator-built and shared; for a user the page is read-only
  (no New, no buttons) — it exists so the user can see what base images they
  can rebuild onto.
- **Snapshots** are created from the Machine detail (`Actions ▾ → Snapshot`).
  Each row's `MACHINE` links back to its VM. The only row action (on
  `Available`) is **Delete** under a row `Dropdown`; Restore/Clone live on the
  Machine detail so there's one mental model: you operate a machine from its
  own page.

## 6. Auth boundary

`/dashboard` requires a logged-in user holding the **`Atlas User`** role.
Guests get Frappe's standard `/login?redirect-to=/dashboard`. The SPA shows
only the owner's rows (the permission query keys on `owner`). No bespoke auth
code — Frappe's session + role gate the page.
