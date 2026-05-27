# Virtual Machine Image — solution

Maps to [research/04-virtual-machine-image.md](../research/04-virtual-machine-image.md).

## 1. `Sync to Server` shows `+ Create a new Server` in the picker

### Problem
The Link picker uses Frappe's default behavior, which includes "Create
a new …" as an option. Spinning up a new server from an image-sync
dialog is a multi-hundred-dollar slip of the wrist.

### Solution

`frappe.ui.form.ControlLink` accepts a `only_select` option that
removes the "Create a new" affordance. Pass it through the dialog
field:

```js
fields: [
    {
        fieldname: "server_name",
        label: __("Server"),
        fieldtype: "Link",
        options: "Server",
        only_select: true,                          // ← removes "+ Create"
        reqd: 1,
        get_query() {
            return { filters: { status: "Active" } };
        },
    },
],
```

`get_query` also filters to **Active** servers — syncing to a
`Pending`/`Bootstrapping` server is wrong (the bootstrap installs
firecracker; until that finishes the sync target doesn't exist on
disk). The picker pre-filters out invalid choices.

### Wireframe

```
Before:                                          After:
┌──── Sync to Server ────────────┐               ┌──── Sync to Server ────────────┐
│ Server *                       │               │ Server *                       │
│ ┌───────────────────────────┐  │               │ ┌───────────────────────────┐  │
│ │                         ▾ │  │               │ │                         ▾ │  │
│ └───────────────────────────┘  │               │ └───────────────────────────┘  │
│  bootstrap-server-1779879805   │               │  bootstrap-server-1779879805   │
│  + Create a new Server  ← BAD  │               │  Advanced Search               │
│  Advanced Search               │               │                                │
└────────────────────────────────┘               └────────────────────────────────┘
                                                  (Only active servers shown.)
```

### Frappe components used
- `only_select: 1` on the Link control.
- `get_query` filtering to `status = "Active"`.

### Fighting Desk?
No.

---

## 2. `Sync to All Servers` has no confirmation or count preview

### Problem
One click, every active server pulls down a multi-GB image. No preview
of "this will hit N servers".

### Solution

Replace the bare `frm.call("sync_to_all_servers")` with a
`frappe.warn`-style confirmation. The `before_show` step counts active
servers and total bytes (`kernel size + rootfs size`, fetched via a HEAD
to the URLs the first time the form opens — cached on the image row).

The dialog body:

```
This will sync ubuntu-24.04 to N active server(s):

  • bootstrap-server-1779879805 (blr1)
  • bootstrap-server-1779879806 (sgp1)
  ...

Each sync downloads ~620 MB (kernel + rootfs) over the public internet
to the server, verifies the SHA-256, and runs sync-image.sh. Expect
~3 min per server.
```

### Wireframe

```
┌─────────────────────────── Sync to All Servers ───────────────────────┐
│  ⚠   Sync to N active servers?                                        │
│                                                                       │
│  Image: ubuntu-24.04  (kernel 30 MB + rootfs 590 MB ≈ 620 MB)        │
│                                                                       │
│  Targets:                                                             │
│    • bootstrap-server-1779879805   blr1   Active                     │
│    • bootstrap-server-1779879806   sgp1   Active                     │
│    • bootstrap-server-1779879807   nyc3   Active                     │
│                                                                       │
│  Each download takes ~3 min and consumes 620 MB of bandwidth.         │
│                                                                       │
│                                       [ Cancel ]   [ Sync to All ]    │
└───────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- Standard `frappe.ui.Dialog` (with `indicator: "orange"`).
- `frappe.db.get_list("Server", {status: "Active"}, ["name", "region"])`
  in `before_show`.

### Fighting Desk?
No.

---

## 3. No sync status

### Problem
The form has no field that says "this image is on these servers". The
operator has to grep Task history.

### Solution

Add a **client-side dashboard panel** showing which servers currently
host the image, derived from Task history. The view is denormalized at
read time, not stored — staying faithful to the spec's "the Frappe site
is the source of truth, but server state is a cache" principle.

Server method:

```python
@frappe.whitelist()
def sync_status(self) -> list[dict]:
    """For each Active server, the last successful sync-image.sh Task
    whose variables include this image. None if never synced."""
    ...
```

Returned shape:

```json
[
  {"server": "bootstrap-server-…", "synced_at": "…", "task": "7do1vheq4m"},
  {"server": "bootstrap-server-…", "synced_at": null, "task": null}
]
```

Rendered as an HTML block above the Workloads section:

```
Synced on
┌──────────────────────────────────────────────────────────────────────┐
│  Server                          Last sync       Task                │
│  bootstrap-server-1779879805     2h ago          7do1vheq4m →        │
│  bootstrap-server-1779879806     (never)         Sync now →          │
│  bootstrap-server-1779879807     3d ago          aa12bb34cd →        │
└──────────────────────────────────────────────────────────────────────┘
```

"Sync now" is a per-row shortcut that opens the existing Sync to Server
dialog with the server pre-filled.

### Wireframe

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⌂ / Virtual Machine Image / ubuntu-24.04                             │
├──────────────────────────────────────────────────────────────────────┤
│  Actions ▾  Sync to Server   Sync to All                       Save  │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  Synced on                                                           │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Server                          Last sync       Task         │   │
│  │ bootstrap-server-…              2h ago          7do1vheq4m → │   │
│  │ bootstrap-server-…              (never)         Sync now →   │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  Workloads                                                           │
│  ┌─────────────────────────────┐                                    │
│  │  Virtual Machine          4 │                                    │
│  └─────────────────────────────┘                                    │
│                                                                      │
│  Description           Is Active                                     │
│  ...                                                                 │
└──────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- New whitelisted method `Virtual Machine Image.sync_status()`.
- HTML field in the form (no doctype-schema change required — the
  client script renders into a `frm.dashboard` HTML region or
  `frm.fields_dict["...html_field..."]`). Easier alternative: add a
  small "Sync Status" Section with an HTML field to the doctype JSON.

### Fighting Desk?
No.

---

## 4. Kernel/Rootfs URLs + SHA-256 are editable post-creation

### Problem
If they're editable, changing the SHA after sync silently invalidates
the audit (old syncs claim a different digest than the image now
records). If they're not editable, they should be visibly read-only.

### Solution

Per spec, `image_name` is `set_only_once`. The kernel/rootfs URLs and
SHAs are *not* — but operationally they should behave as such once any
successful sync exists.

Apply at the controller level:

```python
LOCKED_AFTER_SYNC = (
    "kernel_url", "kernel_filename", "kernel_sha256",
    "rootfs_url", "rootfs_filename", "rootfs_sha256",
)

def validate(self):
    if self.is_new():
        return
    if self._has_successful_sync():
        original = self.get_doc_before_save()
        for field in LOCKED_AFTER_SYNC:
            if getattr(self, field) != getattr(original, field):
                frappe.throw(
                    f"{field} cannot change after the image has been synced. "
                    f"Create a new image (e.g. ubuntu-24.04-v2) instead."
                )

def _has_successful_sync(self) -> bool:
    return frappe.db.exists(
        "Task",
        {"script": "sync-image.sh", "status": "Success",
         "variables": ("like", f'%"IMAGE_NAME": "{self.name}"%')},
    )
```

Client side: when `_has_successful_sync` returns True, the client
script sets each locked field to read-only and renders a small intro:

```
ⓘ  This image has been synced. To change kernel or rootfs, create a
   new image (e.g. ubuntu-24.04-v2). Editing here would invalidate
   prior audit rows.
```

### Wireframe

```
┌──────────────────────────────────────────────────────────────────────┐
│  ⓘ  This image has been synced. Create a new image to change        │
│      kernel or rootfs.                                               │
│  ─────────────────────────────────────────────────────────────────  │
│                                                                      │
│  Kernel                                                              │
│  Kernel URL                            Kernel SHA-256                │
│  ┌──────────────────────────┐ (locked) ┌──────────────────────────┐  │
│  │ https://s3...            │          │ 27a8310b9a727517e9eb...  │  │
│  └──────────────────────────┘          └──────────────────────────┘  │
│                                                                      │
│  Kernel Filename            (locked)                                 │
│  ┌──────────────────────────┐                                       │
│  │ vmlinux-6.1.128          │                                       │
│  └──────────────────────────┘                                       │
└──────────────────────────────────────────────────────────────────────┘
```

### Frappe components used
- `frm.set_df_property(fieldname, "read_only", 1)` on the client.
- Server-side `validate` enforces the same; client only mirrors UX.
- `frm.set_intro(html, "blue")`.

### Fighting Desk?
No.
