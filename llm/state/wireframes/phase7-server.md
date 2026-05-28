# Phase 7 — Server (post-implementation wireframe)

Touches: schema (UUID autoname + title field, drop server_name, drop
`set_only_once` from fields that the DigitalOcean provision flow
populates lazily), controller (autoname() mints a UUID; immutability
list renames `server_name` → `title`), JS (confirm dialogs read
`frm.doc.title`), list (formatter targets `title`), provider
controller + dialog (kwarg renamed `server_name` → `title`; returns
UUID), pre_model_sync patch (`server_name` column → `title`; mints
UUIDs and rewrites FK references in raw SQL), bootstrap (passes a
title, captures the returned UUID).

## Form layout (saved server)

```
┌────────────────────────────────────────────────────────────────────────────┐
│  Server / acme-blr1-01                       Bootstrap (primary, Active≠)  │
│     UUID: 7f3a…b91a (in URL, not on screen)  Re-bootstrap (Actions ▸)      │
│                                              Sync Image (Actions ▸)        │
│                                              Archive (Actions ▸)           │
│                                              Reboot (Actions ▸, danger)    │
├────────────────────────────────────────────────────────────────────────────┤
│  Overview                                                                   │
│  ─────────────                                                              │
│  Title          [acme-blr1-01]              (read-only, set_only_once)      │
│  Provider       [bootstrap-provider]        (read-only, set_only_once)      │
│  Status         (pill)  Active                                              │
│                                                                             │
│  ▸ Provider resource (collapsible)                                          │
│    Provider Resource ID  [4327182]          (read-only)                     │
│    Region                [blr1]             (read-only)                     │
│    Size                  [s-2vcpu-4gb-intel] (read-only)                    │
│                                                                             │
│  ▸ Networking (collapsible)                                                 │
│    IPv4 Address          [203.0.113.5]      (read-only)                     │
│    IPv6 Address          [2a03:b0c0:…::1]   (read-only)                     │
│    IPv6 Prefix (/64)     [2a03:b0c0:…::/64] (read-only)                     │
│    IPv6 VM Range (/124)  [2a03:b0c0:…::/124] (read-only)                    │
│                                                                             │
│  ▸ Host info (collapsible)                                                  │
│    Architecture          [x86_64]           (read-only)                     │
│    Firecracker Version   [v1.15.1]          (read-only)                     │
│    Kernel Version        [6.1.128]          (read-only)                     │
│                                                                             │
│  ▸ Notes (collapsible)                                                      │
│    Notes                 [free text]                                        │
└────────────────────────────────────────────────────────────────────────────┘

(below the form)

┌────────────────────────────────────────────────────────────────────────────┐
│  Connections                                                                │
│  Operations:  12 Tasks  →  (linked to Task list filtered by this Server)    │
│  Virtual Machines:  3   →  (linked to VM list filtered by this Server)      │
└────────────────────────────────────────────────────────────────────────────┘
```

No bespoke "Recent Tasks" panel. Frappe's Connections dashboard panel
(`Operations`) already exposes the Task count + link. The yellow
in-flight headline alert is still rendered when there's a Pending or
Running Task for this server (driven by the `task_update` realtime
event).

## Reboot dialog

```
┌──────────────────────────────────────────────────┐
│  Reboot acme-blr1-01?                            │
│                                                  │
│  Type the server title to confirm                │
│  [_______________]                               │
│                                                  │
│                              [Cancel]  [Reboot]  │  ← red when enabled
└──────────────────────────────────────────────────┘
```

Body is empty (no caveat copy). Typing the title is the entire
deterrent. Same shape as Archive and the VM Terminate dialogs.

## Archive dialog

```
┌──────────────────────────────────────────────────┐
│  Archive acme-blr1-01?                           │
│                                                  │
│  Type the server title to confirm                │
│  [_______________]                               │
│                                                  │
│                              [Cancel]  [Archive] │  ← red when enabled
└──────────────────────────────────────────────────┘
```

Calls `Server.archive()` which sets `status = "Archived"` via
`db.set_value`. The row stays in place — existing FK references from
Tasks and Virtual Machines remain queryable.

## Sync Image dialog

```
┌──────────────────────────────────────────────────┐
│  Sync Image                                      │
│                                                  │
│  Image                                           │
│  [ubuntu-24.04 ▾]   (Link, is_active=1 filter)   │
│                                                  │
│                              [Cancel]  [Sync]    │
└──────────────────────────────────────────────────┘
```

Calls `Server.sync_image(image)` → `VirtualMachineImage.sync_to_server(self.name)`.
The Server form shows a toast and routes to the spawned Task.

## Provision Server dialog (lives on Server Provider)

```
┌──────────────────────────────────────────────────────────────────┐
│  Provision Server                                                 │
│                                                                   │
│  Title         [acme-blr1-02]                                     │
│                lowercase + digits + hyphens, max 63 chars         │
│                                                                   │
│  Region        [blr1 ▾]    (Select; default = provider default)   │
│  Size          [s-2vcpu-4gb-intel ▾]                              │
│  Image         [ubuntu-24-04-x64 ▾]                               │
│                                                                   │
│                                       [Cancel]  [Provision]       │
└──────────────────────────────────────────────────────────────────┘
```

`provision_server(title=…, region=…, size=…, image=…)` returns the new
row's UUID `name`. The client uses the UUID to `frappe.set_route` to
the new form; the operator sees `Server / <title>` because the form
renders the title (and the browser tab title is also the title, via
`set_window_title`).

## List view

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Server                                                + Add Server      │
├──────────────────────────────────────────────────────────────────────────┤
│  Title              Provider                Status   Region   IPv4       │
│  ─────────────────  ────────────────────    ──────   ──────   ────────── │
│  acme-blr1-01       bootstrap-provider      Active   blr1     203.0.113.5│
│  acme-fra1-09       bootstrap-provider      Broken   fra1     203.0.114.1│
└──────────────────────────────────────────────────────────────────────────┘
```

`title` is the leftmost in-list-view column (formatter appends ` · region`
when `region` is set). The list view never renders the UUID name —
only the title and other fields.

## Auto-rename contract (bench migrate)

The `rename_server_to_uuid` patch (pre_model_sync) runs once. For each
existing Server row:

1. DDL renames the `server_name` column to `title` (preserving every
   row's value).
2. Mints a UUID for the new `name`.
3. Updates `tabServer.name` via raw SQL.
4. Updates `tabTask.server` and `tabVirtual Machine.server` to point at
   the new UUID via raw SQL.
5. (No `frappe.rename_doc` call — at pre_model_sync time the in-memory
   DocType meta still reflects the legacy `autoname: field:server_name`
   rule, which trips `rename_doc`'s autoname machinery on the renamed
   column.)

Idempotent: re-running on an already-UUID-named table is a no-op.

## What changed

1. **Schema** ([server.json](../../../atlas/atlas/doctype/server/server.json))
   - `autoname: "field:server_name"` → `autoname: "hash"`
   - `naming_rule: "By fieldname"` → `naming_rule: "Random"`
   - Renamed `server_name` field → `title` (Data, `set_only_once: 1`,
     `in_list_view: 1`, `reqd: 1`; `unique: 1` dropped — uniqueness is
     now via the autoname UUID)
   - Added `title_field: "title"` at top level
   - Networking and Host info are already collapsible Section Breaks
     (no Tab Breaks below the Overview tab) — no change needed
2. **Controller** ([server.py](../../../atlas/atlas/doctype/server/server.py))
   - Added `autoname()` that sets `self.name = str(uuid.uuid4())`
   - `IMMUTABLE_AFTER_INSERT`: `server_name` → `title`
   - `archive()` / `sync_image()` / `bootstrap()` / `reboot()` /
     `get_scripts()` / `run_task_dialog()` unchanged from Phase 6 state
3. **JS** ([server.js](../../../atlas/atlas/doctype/server/server.js))
   - Confirm dialogs now read `frm.doc.title` instead of `frm.doc.name`
     (the UUID would have been an unhelpful label)
   - Reboot/Archive `match_label` is "Type the server title to confirm"
4. **list_js** ([server_list.js](../../../atlas/atlas/doctype/server/server_list.js))
   - Formatter renamed `server_name(value, ...)` → `title(value, ...)`
5. **Server Provider controller**
   ([server_provider.py](../../../atlas/atlas/doctype/server_provider/server_provider.py))
   - `provision_server(server_name, ...)` → `provision_server(title, ...)`
   - The duplicate-check filter now keys on `title`
     (`frappe.db.exists("Server", {"title": title})`)
   - Returns the inserted row's UUID `name`, not the title
   - The DigitalOcean `create_droplet` call still uses `title` as the
     droplet `name` and tag (DO wants a slug, not a UUID)
6. **Server Provider JS**
   ([server_provider.js](../../../atlas/atlas/doctype/server_provider/server_provider.js))
   - Provision dialog: `server_name` field renamed to `title`
   - Validator renamed `validate_server_name` → `validate_server_title`
   - Toast still shows `values.title`; route uses the returned UUID
7. **Migration patch**
   ([rename_server_to_uuid.py](../../../atlas/patches/v1_0/rename_server_to_uuid.py))
   - Pre_model_sync. Renames the column, mints UUIDs, rewrites FKs in
     raw SQL.
8. **Bootstrap** ([bootstrap.py](../../../atlas/bootstrap.py))
   - `provision_server(title)` returns a UUID; bootstrap stores it as
     `server_name` (the variable name semantically means "the row's
     `name` attribute", now a UUID)
9. **Tests**
   - `fixtures.make_server(provider, title=...)`: signature renamed
     `name` → `title`; lookup is by `{title: title}`.
   - `test_server.py`: `frappe.db.delete("Server", {"server_name": …})`
     → `{"title": …}`; new tests
     `test_title_is_immutable_once_set` and `test_name_is_a_uuid`.
   - `test_server_provider.py`: every test uses `title` as the dialog
     input and `frappe.get_doc("Server", returned)` where `returned` is
     the UUID name.
   - `test_networking.py`: `_provider_and_server(title)` returns the
     server's UUID `name` so the FK filters resolve.
   - `test_virtual_machine_image.py`: `_provider_and_server(title,
     status)` returns the UUID; the sync-Task FK assertions use the
     returned UUID.
   - `test_ssh_runner.py`: `make_server(provider=…, title=…, ...)`
     (kwarg renamed).
   - `test_task.py`: `make_server(title=…)` (kwarg renamed).
10. **E2E harness**
    - `tests/e2e/_droplets.py`: provisioning helper passes a title and
      stores the returned UUID; teardown sweep filters by `title` LIKE.
    - `tests/e2e/use_cases/server_provisioning.py`,
      `tests/e2e/use_cases/desk_buttons.py`,
      `tests/e2e/use_cases/virtual_machine_provisioning.py`,
      `tests/e2e/use_cases/ssh_primitive.py`: same renames.
