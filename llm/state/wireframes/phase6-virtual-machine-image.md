# Phase 6 — Virtual Machine Image (post-implementation wireframe)

Touches: schema (description→title rename, drop sync_status_html,
collapse image-data tab to section), controller (full-field immutability
from insert, after_insert auto-sync, archive), JS (drop primary +
sync-to-all + sync-status-panel), list (drop image_name from list view).

## Form layout (saved image) — read-only post-insert

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Virtual Machine Image / ubuntu-24.04             Save (subtle)          │
│                                                    Archive (Actions ▸)   │
├──────────────────────────────────────────────────────────────────────────┤
│  Overview                                                                │
│  ─────────────────────                                                   │
│  Image Name           [ubuntu-24.04]               (read-only)           │
│  Title                [Firecracker CI Ubuntu 24.04 rootfs] (read-only)   │
│  Is Active            ☑                                                  │
│  Default Disk (GB)    [4]                          (read-only)           │
│                                                                          │
│  ▸ Image data         (collapsible section, was a Tab)                   │
│      ▸ Kernel:                                                           │
│        Kernel URL         [https://…vmlinux-6.1.128]   (read-only)       │
│        Kernel Filename    [vmlinux-6.1.128]            (read-only)       │
│        Kernel SHA-256     [27a8310b…]                  (read-only)       │
│      ▸ Rootfs:                                                           │
│        Rootfs URL         [https://…ubuntu-24.04.sq…]  (read-only)       │
│        Rootfs Filename    [ubuntu-24.04.ext4]          (read-only)       │
│        Rootfs SHA-256     [88821a26…]                  (read-only)       │
└──────────────────────────────────────────────────────────────────────────┘
```

No `Sync to Server` primary button. No `Sync to All Servers` Actions
item. No Sync Status panel. Operator's only verb on a saved image is
`Archive`.

## Auto-sync contract

Inserting a Virtual Machine Image with `is_active=1` triggers
`after_insert` → one `sync-image.sh` Task per Active Server. The Task
list already tracks each attempt; a dedicated `Virtual Machine Image
Sync` DocType (as in the original plan) was deferred — see drift table.

## List view

```
┌──────────────────────────────────────────────────────────────────────┐
│  Virtual Machine Image                                  + Add Image  │
├──────────────────────────────────────────────────────────────────────┤
│  ID                  Title                       Active   Disk (GB)  │
│  ──────────────────  ────────────────────────    ──────  ─────────── │
│  ubuntu-24.04        Firecracker CI Ubuntu 24…   ☑          4        │
│  ubuntu-24.04-v2     Firecracker CI w/ patch…    ☑          4        │
└──────────────────────────────────────────────────────────────────────┘
```

`image_name` is now the ID column (autoname). The legacy duplicate
"Image Name" column is gone (`in_list_view: 0`). Title is the readable
label.

## What changed

1. **Schema** ([virtual_machine_image.json](../../../atlas/atlas/doctype/virtual_machine_image/virtual_machine_image.json))
   - `description` field renamed to `title`; `title_field` added
   - `image_name` lost `in_list_view: 1` (it's the autoname/ID column)
   - `sync_status_html` field removed
   - `section_break_sync_status` removed
   - `tab_image_data` (Tab Break) converted to a collapsible Section Break
   - Every non-`is_active` field gained `set_only_once: 1`
2. **Controller** ([virtual_machine_image.py](../../../atlas/atlas/doctype/virtual_machine_image/virtual_machine_image.py))
   - `LOCKED_AFTER_SYNC` replaced with `IMMUTABLE_AFTER_INSERT`; the
     immutability check now fires from insert, not just from first sync
   - `_has_successful_sync` removed (not needed any more)
   - New `after_insert` enqueues `sync-image.sh` Tasks for every
     Active Server when `is_active=1`
   - New `archive()` whitelisted method
3. **JS** ([virtual_machine_image.js](../../../atlas/atlas/doctype/virtual_machine_image/virtual_machine_image.js))
   - Stripped down to only the `Archive` Actions item
   - Removed: `open_sync_to_server_dialog`, `confirm_sync_to_all`,
     `render_sync_status_panel`, `enforce_lock_state` (the controller
     blocks the change now; the form just renders read-only)
4. **list_js** ([virtual_machine_image_list.js](../../../atlas/atlas/doctype/virtual_machine_image/virtual_machine_image_list.js))
   - `add_fields: ["is_active", "title"]` (was `description`)
   - Formatter renamed `description` → `title`
5. **Migration patch** ([rename_image_description_to_title.py](../../../atlas/patches/v1_0/rename_image_description_to_title.py))
   - Pre-model-sync. DDL renames `description` column to `title`
6. **Tests** ([test_virtual_machine_image.py](../../../atlas/atlas/doctype/virtual_machine_image/test_virtual_machine_image.py))
   - Added `TestVirtualMachineImageAutoSync` (2 tests)
   - Added `TestVirtualMachineImageImmutability` (4 tests)
7. **fixtures.py** — uses `title` instead of `description` from
   `DEFAULT_IMAGE`
8. **bootstrap.py** — `DEFAULT_IMAGE["description"]` →
   `DEFAULT_IMAGE["title"]`
9. **e2e _config.py** — `DEFAULT_IMAGE` dict uses `title`
