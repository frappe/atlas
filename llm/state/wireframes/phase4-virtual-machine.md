# Phase 4 — Virtual Machine (post-implementation wireframe)

Touches: schema (description→title rename, collapse tabs, immutability
flags), controller (after_insert → auto_provision), JS (drop nudge +
Pending primary, simplify Terminate dialog, auto-select Server).

## Form layout (Pending Virtual Machine, fresh insert) — single Overview tab

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Virtual Machine / <title> · 4ee21d36           Save (subtle)            │
│                                                  Terminate (Actions ▸)   │
├──────────────────────────────────────────────────────────────────────────┤
│  IPv6 [2001:db8:1::1]                                  (orange chip)     │
├──────────────────────────────────────────────────────────────────────────┤
│  Overview                                                                │
│  ─────────────────────                                                   │
│  Title *                       [my test vm     ]   (locked after save)   │
│  Server *                      [vm-test-server ▾]  (auto-selected when   │
│                                                     one Active server)   │
│  Image *                       [ubuntu-24.04   ▾]                        │
│  Status                        [Pending        ]   (read-only)           │
│                                                                          │
│  Resources                                                               │
│  ─────────────────────                                                   │
│  Size preset    [Custom ▾]                                               │
│  vCPUs    1     Memory (MB)   512     Disk (GB)   4                      │
│                                                                          │
│  ▸ Security                            (collapsible section)             │
│  ▸ Networking                          (collapsible section)             │
│  ▸ Activity                            (collapsible section)             │
└──────────────────────────────────────────────────────────────────────────┘
```

No "Without a description the list will show only a UUID" yellow nudge
(operator requested removal). Networking and Activity are collapsible
sections under Overview, not separate tabs.

## Terminate dialog

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Terminate <title>?                                            [×]       │
├──────────────────────────────────────────────────────────────────────────┤
│  Type the title to confirm *                                             │
│  [_________________________________]                                     │
│  Type <title> to enable the button below.                                │
│                                                                          │
│                                                  [Cancel]  [Terminate]   │
│                                                            (red, disabled│
│                                                             until match) │
└──────────────────────────────────────────────────────────────────────────┘
```

No IPv6 / Image / Server detail block. The form already shows them.

## Auto-provision contract

Inserting a Pending VM enqueues `auto_provision(virtual_machine_name)`
via `after_insert`. The worker calls `vm.provision()`. The operator
never has to click Provision on a freshly-created Pending row.
`Failed` retains its `Provision` primary button so the operator can
retry after fixing the cause.

## What changed

1. **Schema** ([virtual_machine.json](../../../atlas/atlas/doctype/virtual_machine/virtual_machine.json))
   - `description` field renamed to `title`; `title_field` updated;
     `search_fields` updated
   - `title` and `ssh_public_key` gained `set_only_once`
   - `tab_networking` and `tab_activity` Tab Breaks converted to
     collapsible Section Breaks; `section_break_access` relabeled
     to `Security`
2. **Controller** ([virtual_machine.py](../../../atlas/atlas/doctype/virtual_machine/virtual_machine.py))
   - `IMMUTABLE_AFTER_INSERT` extended with `title`, `ssh_public_key`
   - New `after_insert` hook enqueues `auto_provision` to the long queue
   - New module-level `auto_provision(virtual_machine_name)` worker
     function: no-op if VM is not Pending, else calls `provision()`
3. **JS** ([virtual_machine.js](../../../atlas/atlas/doctype/virtual_machine/virtual_machine.js))
   - Dropped `render_description_nudge` (the yellow banner)
   - Dropped the Pending Provision primary button (auto-fires now);
     `Failed` keeps its Provision primary for retries
   - Capacity headline only fires on the oversubscribed case
   - Terminate dialog body emptied; match string is `title || name`
     instead of the short ID
   - Re-provision-as-new now copies `title` (with " (clone)" suffix)
     instead of description
   - New `auto_select_server` runs on `onload` of a new VM: queries
     Active Servers, sets `server` when exactly one exists
4. **list_js** ([virtual_machine_list.js](../../../atlas/atlas/doctype/virtual_machine/virtual_machine_list.js))
   - `add_fields` → `title` (was `description`); formatter renamed
5. **Migration patch** ([rename_vm_description_to_title.py](../../../atlas/patches/v1_0/rename_vm_description_to_title.py))
   - Pre-model-sync. DDL renames `description` column to `title`;
     idempotent
6. **Tests** ([test_virtual_machine.py](../../../atlas/atlas/doctype/virtual_machine/test_virtual_machine.py))
   - Added `test_after_insert_enqueues_auto_provision`
   - Added `test_auto_provision_is_noop_when_not_pending`
   - Added `test_auto_provision_calls_provision_when_pending`
   - Added `test_title_is_immutable`
   - Added `test_ssh_public_key_is_immutable`
7. **bootstrap.py**
   - `description="bootstrap test vm"` → `title="bootstrap test vm"`
   - `provision_virtual_machine` no longer calls `vm.provision()`
     explicitly; new `_wait_for_provision_task` helper polls for the
     auto_provision-enqueued Task
8. **task.py** / **task.js**
   - `_target_short` reads `title`/`name` instead of `description`/`name`
   - VM indicator in task.js dashboard reads `title`
9. **fixtures.py**
   - `make_virtual_machine` seeds `title="test vm"`
10. **test_networking.py**
    - `_insert_vm` writes `title` instead of `description`
