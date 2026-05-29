# DocTypes

Ten DocTypes. Module `Atlas`. None are submittable. All track changes.
Read permission for `System Manager`.

1. [Atlas Settings](#atlas-settings) — vendor-agnostic Atlas config (Single).
2. [Provider](#provider) — one row per configured vendor.
3. [DigitalOcean Settings](#digitalocean-settings) — DO API config (Single).
4. [Self-Managed Settings](#self-managed-settings) — Self-Managed config (Single).
5. [Provider Size](#provider-size) — vendor catalog of machine sizes.
6. [Provider Image](#provider-image) — vendor catalog of OS images.
7. [Server](#server)
8. [Virtual Machine](#virtual-machine)
9. [Virtual Machine Image](#virtual-machine-image)
10. [Virtual Machine Snapshot](#virtual-machine-snapshot) — a disk snapshot of a VM.
11. [Task](#task)

The first six form the **Provider abstraction**: a single ABC in
`atlas/atlas/providers/base.py` with one implementation per
`Provider.provider_type`. Every vendor call goes through that interface;
controllers never branch on `provider_type`. See
[provider-abstraction.md](../llm/plan/provider-abstraction.md) for the
implementation plan.

Each DocType is specified by three sections: **Fields** (the schema), **Form
layout** (the section/column structure of the desk form), and **List view**
(column order and standard filters). Together these are enough to
regenerate the JSON without consulting the implementation.

Notation in the Form layout sections:

- `── <label> ──` is a Section Break with that label.
- `(collapsible)` after a section label means the section is collapsed by
  default.
- `|` is a Column Break inside a section. Fields after `|` lay out in the
  next column.

---

## Atlas Settings

A Single DocType. Holds Atlas-wide configuration that is not vendor-specific:
which `Provider` is currently active, and the operator's SSH key (fingerprint,
public-key body, on-disk path). Every `get_provider()` call in the codebase
reads `Atlas Settings.provider` to pick the implementation — this is the
indirection layer.

### Fields

| Field                  | Type             | Reqd | Notes                                                              |
| ---------------------- | ---------------- | ---- | ------------------------------------------------------------------ |
| `provider`             | Link → Provider  | Y    | The currently-active Provider row. `atlas.get_provider()` reads this. |
| `ssh_fingerprint`      | Data             |      | Vendor-side fingerprint of the SSH key, when the vendor needs one (DigitalOcean). Format is vendor-specific (DO uses SHA-256 hex). |
| `ssh_public_key`       | Long Text        |      | OpenSSH public key body. Crosses the provider interface for vendors that upload at provision time. Not required for DO. |
| `ssh_private_key_path` | Data             | Y    | Absolute path on the Atlas host where the matching private key lives. Atlas reads the PEM at SSH-connect time via `secrets.get_ssh_key_from_disk(path)`. `0600`, owned by the Frappe user. |

Why one Single instead of fields on each `Provider` row: the SSH key, the
active provider, and any other cross-vendor switch are properties of the
Atlas instance, not of a vendor. Routing reads through a single helper
also lets the storage backend swap to an external secret store later
without touching callers.

### Form layout

```
── Active provider ──
provider
── SSH key ──
ssh_fingerprint
ssh_public_key
| ssh_private_key_path
```

### Buttons

None. The form saves on edit and `atlas.get_provider()` picks up the new
value on the next call. Switching the active provider does not destroy
any existing Server rows — they keep their `provider` FK pointing at
whatever they were provisioned through.

---

## Provider

One row per configured vendor. Thin link table — no credentials, no
defaults. Vendor-specific configuration lives on the per-vendor Single
Settings DocType (e.g. `DigitalOcean Settings`).

`Server.provider` is a Link → Provider, frozen on first save.

### Fields

| Field           | Type   | Reqd | Default | Notes                                                              |
| --------------- | ------ | ---- | ------- | ------------------------------------------------------------------ |
| `provider_name` | Data   | Y    |         | Primary key. Unique. `set_only_once`. e.g. `digitalocean-production`, `home-lab`. |
| `provider_type` | Select | Y    |         | Options: `DigitalOcean`, `Self-Managed`. `set_only_once`. The provider registry (`atlas/atlas/providers/__init__.py`) keys off this value to pick the implementation class. |
| `is_active`     | Check  |      | 1       | Flipped to 0 via the `archive()` controller method. `get_provider()` refuses to instantiate an archived row. |

### Form layout

```
provider_name
provider_type
| is_active
```

### List view

- Columns: `provider_name`, `provider_type`, `is_active`.
- Standard filters: `provider_type`, `is_active`.

### Buttons

- **Provision Server** (primary) — opens a dialog. Common field:
  `title` (lowercase + digits + hyphens, max 63 chars; passed through
  to the vendor as the server's name and tag). The remaining inputs
  are produced by the provider implementation's `discover()`-backed
  dialog schema:
  - **DigitalOcean**: `size` (Link → Provider Size, filtered to
    `provider_type=DigitalOcean, enabled=1`), `image` (Link → Provider
    Image, same filter), defaulting to `DigitalOcean Settings.default_size`
    / `default_image`. Then `confirm_cost` ("Create a billable
    droplet?") before the DO API call.
  - **Self-Managed**: `ipv4_address`, `ipv6_address`, `ipv6_prefix`,
    `ipv6_virtual_machine_range`. Atlas inserts the `Server` directly
    with the operator-supplied values and runs bootstrap. No API call.
  The whitelisted `provision_server(title, ...)` controller method
  returns the new Server row's UUID `name`. See
  [03-bootstrapping.md](./03-bootstrapping.md).
- **Authenticate** — under `Actions ▾`. Calls `provider.authenticate()`,
  which probes the vendor (DigitalOcean: `GET /account`) and reports
  back account label, rate-limit headers, and `missing_scopes`. Hidden
  for vendors without remote credentials (Self-Managed returns
  `ok=True, account_label="local"` so the form still paints a green
  chip on refresh).
- **Refresh Catalog** — under `Actions ▾`. Calls `provider.discover()`
  and upserts `Provider Size` and `Provider Image` rows. Slugs the
  vendor no longer returns are flipped to `enabled=0`; historical
  Server rows keep their Link.
- **Archive** — `Actions ▾`, shown only when `is_active = 1`. Calls
  `archive()` which flips `is_active = 0` via `db.set_value`. Existing
  Servers keep their FK reference so historical Tasks stay queryable.

The Provider form has no auto-painted credential indicator; the
operator clicks **Authenticate** when they need to verify.

---

## DigitalOcean Settings

A Single DocType. Only fields that DigitalOcean's API needs.

### Fields

| Field           | Type                  | Reqd | Notes                                                              |
| --------------- | --------------------- | ---- | ------------------------------------------------------------------ |
| `api_token`     | Password              | Y    | `set_only_once`. DigitalOcean personal access token. Rotate by clearing the field via `db.set_value`, then re-saving. |
| `region`        | Data                  | Y    | DO is multi-region; Atlas is single-region. Pick one (`blr1`, `nyc3`, …). `provision_server` throws if the dialog overrides this. |
| `default_size`  | Link → Provider Size  | Y    | Filtered to `provider_type=DigitalOcean, enabled=1`. Default selection in the Provision dialog. |
| `default_image` | Link → Provider Image | Y    | Same filter as `default_size`.                                     |

### Form layout

```
api_token
region
── Defaults for new servers ──
default_size
| default_image
```

### Buttons

- **Test Connection** — under `Actions ▾`. Calls
  `DigitalOceanProvider.authenticate()` (same as the Provider form's
  Authenticate button, mirrored here for the operator who's mid-credentials).
  Result surfaces via a toast (`OK: <account>` / `Failed: <error>`);
  there is no auto-painted dashboard indicator.

Monthly cost preview for the Provision dialog reads `Provider Size.monthly_cost_usd`
directly. Sizes without a cost render as "—" rather than guess.

---

## Self-Managed Settings

A Single DocType. Empty stub today — Self-Managed has no vendor-side
configuration (everything Atlas needs for a Self-Managed host lives in
`Atlas Settings` plus the operator-supplied IPs at provision time).
The DocType exists so future Self-Managed-only knobs have a home; the
form ships with a single section break and no fields.

---

## Provider Size

A regular DocType. One row per vendor-advertised machine size that Atlas
is willing to provision. Seeded at first run by `bootstrap.py` and
refreshed via the Provider form's **Refresh Catalog** button (which
calls `provider.discover()`).

### Fields

| Field               | Type   | Reqd | Read-only | Default | Notes                                                              |
| ------------------- | ------ | ---- | --------- | ------- | ------------------------------------------------------------------ |
| `name`              | Data   | Y    | Y         |         | Primary key. Format: `{provider_type}/{slug}` (e.g. `DigitalOcean/s-2vcpu-4gb-intel`). Assigned in `autoname()`. |
| `provider_type`     | Select | Y    |           |         | Same options as `Provider.provider_type`. `set_only_once`.         |
| `slug`              | Data   | Y    |           |         | Vendor-native slug — the string sent on the API wire (`s-2vcpu-4gb-intel`). `set_only_once`. |
| `enabled`           | Check  |      |           | 1       | Flipped by `discover()` when the vendor drops a slug. Disabled rows do not appear in the Provision dialog but remain pointable from historical Server rows. |
| `monthly_cost_usd`  | Int    |      |           |         | Hand-maintained for vendors without per-size pricing in the API (DO). Renders as "—" when blank. |
| `provider_metadata` | Code (JSON) |  | Y      |         | Raw vendor response for this size — vCPU count, RAM, disk tier, anything the vendor returns. Read-only on the form. |

### List view

- Columns: `slug`, `provider_type`, `enabled`, `monthly_cost_usd`.
- Standard filters: `provider_type`, `enabled`.

---

## Provider Image

A regular DocType. One row per vendor-advertised OS image that Atlas is
willing to provision a server with. Same lifecycle as `Provider Size`.

Distinct from `Virtual Machine Image`: this is the *server's* base image
(the OS Atlas bootstraps on top of), not the guest rootfs+kernel pair
that runs inside a Firecracker microVM.

### Fields

| Field               | Type   | Reqd | Read-only | Default | Notes                                                              |
| ------------------- | ------ | ---- | --------- | ------- | ------------------------------------------------------------------ |
| `name`              | Data   | Y    | Y         |         | Primary key. Format: `{provider_type}/{slug}` (e.g. `DigitalOcean/ubuntu-24-04-x64`). |
| `provider_type`     | Select | Y    |           |         | `set_only_once`.                                                   |
| `slug`              | Data   | Y    |           |         | Vendor-native slug (DO `ubuntu-24-04-x64`, future AWS `ami-…`). `set_only_once`. |
| `enabled`           | Check  |      |           | 1       | Flipped by `discover()`.                                           |
| `provider_metadata` | Code (JSON) |  | Y      |         | Raw vendor response — architecture, distribution, release date, …  |

### List view

- Columns: `slug`, `provider_type`, `enabled`.
- Standard filters: `provider_type`, `enabled`.

---

## Server

One row per host. The primary key is a UUID assigned at insert; the
operator-facing label lives in `title` (e.g. `server-blr1-01`).

### Fields

| Field                          | Type                  | Reqd | Read-only | Default | Notes                                                          |
| ------------------------------ | --------------------- | ---- | --------- | ------- | -------------------------------------------------------------- |
| `name`                         | UUID (autoname)       | Y    | Y         |         | Primary key. UUID minted in `Server.autoname()`. Stable for the row's lifetime; no rename UI. |
| `title`                        | Data                  | Y    |           |         | Operator-chosen label. `set_only_once` — first save freezes it. |
| `provider`                     | Link → Provider       | Y    |           |         | `set_only_once`. |
| `status`                       | Select                | Y    | Y         | Pending | `Pending`, `Bootstrapping`, `Active`, `Draining`, `Broken`, `Archived`. Controllers mutate via `db.set_value`. |
| `provider_resource_id`         | Data                  |      | Y         |         | Vendor's primary key for this host (DigitalOcean droplet id, future AWS instance id, …). Empty for `Self-Managed`. Locked once written. |
| `size`                         | Link → Provider Size  |      | Y         |         | Populated by `provider.describe()` after provision. Empty for `Self-Managed`. |
| `image`                        | Link → Provider Image |      | Y         |         | Server's base OS image. Populated by `provider.describe()`. Empty for `Self-Managed`. |
| `ipv4_address`                 | Data                  |      | Y         |         | The SSH endpoint. Set by `provider.describe()` (DigitalOcean) or by the operator at provision time (Self-Managed). Locked once written. |
| `ipv6_address`                 | Data                  |      | Y         |         | The server's own IPv6. Whatever the host actually answers on. |
| `ipv6_prefix`                  | Data                  |      | Y         |         | The full prefix routed to this server (typically a /64). Informational. |
| `ipv6_virtual_machine_range`   | Data                  |      | Y         |         | The subnet Atlas allocates VM addresses from. Any prefix length: `/64`, `/80`, `/124`, ... Produced by `provider.describe()`. For `DigitalOcean` it's the /124 carved from the /64 (see [06-networking.md](./06-networking.md)); for `Self-Managed` it's the operator-typed value. |
| `provider_metadata`            | Code (JSON)           |      | Y         |         | Raw vendor blob returned by `describe()`. Holds anything the vendor reports that doesn't have a named column (DigitalOcean `created_at`, future AWS placement group, …). Forward-compatibility seam — read-only. |
| `architecture`                 | Data                  |      | Y         |         | Set by bootstrap. Allowed to change on re-bootstrap. |
| `firecracker_version`          | Data                  |      | Y         |         | Set by bootstrap. Allowed to change on re-bootstrap. |
| `kernel_version`               | Data                  |      | Y         |         | Set by bootstrap. Allowed to change on re-bootstrap. |
| `notes`                        | Text                  |      |           |         |                                                                |

Atlas is single-region: there is no `Server.region` column. A vendor
that operates in multiple regions stores its operating region on its
own Settings Single (e.g. `DigitalOcean Settings.region`), and one
Atlas instance pins one region per vendor.

Immutability is enforced by `Server._validate_immutability()` (lock
once a value is written; allow `None → value` so `finish_provisioning`
can write the IPs, size, image, and `provider_metadata` onto a freshly
inserted Pending row). The framework `set_only_once` flag covers
`title` and `provider` because those are populated at insert time and
never legitimately change.

### Controller methods

- `archive()` — calls `provider.destroy(provider_resource_id)` first
  (no-op for Self-Managed, releases the droplet for DigitalOcean), then
  sets `status = "Archived"` via `db.set_value`. Idempotent (rejects
  if already Archived). Existing FKs from Virtual Machine and Task rows
  are preserved.
- `sync_image(image)` — single-server convenience wrapper around
  `Virtual Machine Image.sync_to_server(self.name)`. Used by the
  Server form's Sync Image action.
- `bootstrap()` / `reboot()` / `get_scripts()` / `run_task_dialog(...)`
  — Task-running entry points; see [04-tasks.md](./04-tasks.md).

The split between `ipv6_prefix` and `ipv6_virtual_machine_range` is
because on DigitalOcean a /64 is advertised but only the first /124 is
actually routable; we hand out addresses inside that /124 only. On
Self-Managed hosts the operator might have an entire extra /64 (or /80,
or /48) routed to the box and so the VM range can be much larger than
/124. Atlas treats `ipv6_virtual_machine_range` as "the subnet I am
allowed to allocate from" and does not try to derive it. Details in
[06-networking.md](./06-networking.md).

### Form layout

Single `Overview` tab. Networking / Host info / Notes are collapsible
sections, not separate tabs.

```
── Overview ──
title
provider
| status
── Provider resource ──
provider_resource_id
| size
  image
── Networking (collapsible) ──
ipv4_address
ipv6_address
| ipv6_prefix
  ipv6_virtual_machine_range
── Host info (collapsible) ──
architecture
| firecracker_version
  kernel_version
── Provider metadata (collapsible) ──
provider_metadata
── Notes (collapsible) ──
notes
```

### List view

- Columns (left to right): `title`, `provider`, `status`, `size`,
  `ipv4_address`.
- Standard filters: `provider`, `status`, `size`.

### Buttons

- **Bootstrap** (primary on `Pending` / `Bootstrapping` / `Broken`;
  folds under `Actions ▾` as **Re-bootstrap** on `Active`) — runs
  [`scripts/bootstrap-server.sh`](../scripts/bootstrap-server.sh).
  Idempotent.
- **Sync Image** (under `Actions ▾`, on `Active`) — opens a one-field
  dialog (Link to `Virtual Machine Image`) and calls
  `Server.sync_image(image)`. There is no operator-driven "Run Task"
  catch-all on the form; lifecycle scripts that aren't a first-class
  button live on the relevant DocType (VM start/stop on the VM form,
  etc.). The `run_task_dialog` controller method is kept for
  `Task.retry` only.
- **Archive** (under `Actions ▾`, on non-`Archived` rows, danger) —
  confirms via a type-the-title dialog. Calls
  `provider.destroy(provider_resource_id)` first (releases the
  DigitalOcean droplet; no-op for Self-Managed), then sets
  `status = "Archived"`. Archive is the destroy trigger — there is no
  separate Destroy button. The dialog body warns that the vendor
  resource will be released.
- **Reboot** (under `Actions ▾`, danger) — runs
  [`scripts/reboot-server.sh`](../scripts/reboot-server.sh)
  (`systemctl reboot` over SSH). The resulting Task may end in `Failure`
  (SSH drops before the script returns) or `Success` (`systemctl reboot`
  exits before the connection is torn down). Either outcome is normal; the
  meaning is "the server is rebooting." Operators confirm reboot by
  watching for SSH to come back, not by reading the Task status. The
  desk requires the operator to type the server title into a
  text-match dialog before the red button enables — see
  [10-desk-ui.md](./10-desk-ui.md).

Frappe's standard Connections dashboard renders below the form, linking
Virtual Machines and Tasks via their `server` field (configured in
`server_dashboard.py`). The desk's bespoke "Recent Tasks" quick_list
has been removed — Operations on the Connections dashboard already
exposes the same information.

---

## Virtual Machine

One row per microVM. The primary key is a UUID assigned at insert and never
changes — not even on terminate. Predictable, stable identity that survives
deletion.

### Fields

| Field              | Type                          | Reqd | Read-only | Default | Notes                                                            |
| ------------------ | ----------------------------- | ---- | --------- | ------- | ---------------------------------------------------------------- |
| `name`             | UUID                          | Y    | Y         |         | Primary key. Set in `before_insert` via `uuid.uuid4()`.          |
| `title`            | Data                          | Y    |           |         | Operator-chosen label; `title_field` for the form. `set_only_once`. |
| `server`           | Link → Server                 | Y    |           |         | `set_only_once` (in addition to the controller's `_validate_immutability`). |
| `image`            | Link → Virtual Machine Image  | Y    |           |         | `set_only_once` (in addition to the controller's `_validate_immutability`). |
| `status`           | Select                        | Y    | Y         | Pending | `Pending`, `Running`, `Paused`, `Stopped`, `Failed`, `Terminated`. Driven by lifecycle methods only. |
| `vcpus`            | Int                           | Y    |           | 1       | Frozen on ordinary saves; mutable via `resize()` on a Stopped VM. No `set_only_once` (the controller is the gate). |
| `memory_megabytes` | Int                           | Y    |           | 512     | Same resize rule as `vcpus`.                                     |
| `disk_gigabytes`   | Int                           | Y    |           | 4       | Same resize rule. Resize may only grow it.                       |
| `ssh_public_key`   | Long Text                     | Y    |           |         | `set_only_once`. Injected into the rootfs.                       |
| `clone_source_rootfs` | Data                       |      | Y         |         | Internal, hidden. On-host snapshot rootfs to seed this VM's disk from (clone). Empty for a normal image-backed VM. `set_only_once`, `no_copy`. |
| `ipv6_address`     | Data                          |      | Y         |         | From the server's /124. Set in `before_insert`.                  |
| `mac_address`      | Data                          |      | Y         |         | Derived from `name`. Set in `before_validate`.                   |
| `tap_device`       | Data                          |      | Y         |         | Derived from `name`. Set in `before_validate`.                   |
| `last_started`     | Datetime                      |      | Y         |         |                                                                  |
| `last_stopped`     | Datetime                      |      | Y         |         |                                                                  |

Because the name is a UUID, the operator needs `title` to recognize a
VM in lists. The framework's `title_field` points at it; the browser
tab, breadcrumb, and list-view subject all read `title`.

`status` is read-only on the form because it is only ever set by lifecycle
methods (Provision/Start/Stop/Restart/Terminate); see
[05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md).

`ssh_public_key` is the key injected into the *guest's*
`/root/.ssh/authorized_keys` — it is how the operator SSHes into the
VM, not into the host. The host key lives on `Atlas Settings`
(`ssh_fingerprint`, `ssh_public_key`, `ssh_private_key_path`).

### Auto-provision contract

`Virtual Machine.after_insert` enqueues
`atlas.atlas.doctype.virtual_machine.virtual_machine.auto_provision`
on the `long` queue; the worker resolves the VM by name, checks
`status == "Pending"`, and calls `provision()`. The operator clicks
**Save**, not **Provision** — the form's Pending state no longer
carries a primary action. A failed auto-provision flips the VM to
`Failed`, at which point the form's **Provision** primary returns as
a retry. See [05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md).

### Form layout

A single `Overview` Tab Break with the rest as collapsible Section
Breaks (the old `Networking` / `Activity` tabs folded in):

```
title
server
image
| status
── Resources ──
vcpus
| memory_megabytes
| disk_gigabytes
── Security ── (collapsible)
ssh_public_key
── Networking ── (collapsible)
ipv6_address
| mac_address
  tap_device
── Activity ── (collapsible)
last_started
| last_stopped
```

### List view

- Columns (left to right): `title`, `server`, `image`, `status`,
  `ipv6_address`.
- Standard filters: `server`, `image`, `status`.

### Buttons

Tiering is keyed off `status` — see [10-desk-ui.md § Virtual Machine](./10-desk-ui.md#virtual-machine):

- **Pending** — no primary; `after_insert` already enqueued provision.
- **Provision** (primary on `Failed`) — manual retry after an
  auto-provision failure. Runs
  [`scripts/provision-vm.sh`](../scripts/provision-vm.sh).
- **Start** (primary on `Stopped`) — `Stopped` → `Running`.
- **Stop** (primary on `Running`) — `Running` → `Stopped`. Also offered
  (secondary) on `Paused`.
- **Resume** (primary on `Paused`) — `Paused` → `Running`.
- **Restart** (secondary on `Stopped` / `Running`) → `Running`.
- **Pause** (secondary on `Running`) — `Running` → `Paused` via the API
  socket. Runs [`scripts/pause-vm.sh`](../scripts/pause-vm.sh).
- **Snapshot / Rebuild / Resize** (secondaries on `Stopped`, each opens a
  dialog) — disk and size operations; see
  [05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md). They
  appear only while Stopped, which is the deterrent against resizing or
  snapshotting a live VM (the controllers also enforce it).
- **Terminate** (under `Actions ▾`, danger; available until
  `Terminated`) — runs
  [`scripts/terminate-vm.sh`](../scripts/terminate-vm.sh), sets
  `status = Terminated` and deletes the VM's snapshot rows. The UUID does
  not change. The desk requires the operator to type the VM's `title` into a
  `confirm_destructive` dialog before the red button enables; the dialog body
  is empty — typing the title is the entire deterrent.

---

## Virtual Machine Snapshot

A disk snapshot of one VM — a copy of its `rootfs.ext4` at a point in time.
Not a Firecracker memory-state snapshot. Created from a Stopped VM; the bytes
live on the same server as the VM, under
`/var/lib/atlas/virtual-machines/<vm-uuid>/snapshots/<snapshot-uuid>/`.

### Fields

| Field             | Type                          | Reqd | Read-only | Default | Notes                                                            |
| ----------------- | ----------------------------- | ---- | --------- | ------- | ---------------------------------------------------------------- |
| `name`            | UUID (autoname `hash`)        | Y    | Y         |         | Primary key; names the on-host snapshot directory.               |
| `title`           | Data                          | Y    |           |         | Operator label. `title_field`. `set_only_once`.                  |
| `virtual_machine` | Link → Virtual Machine        | Y    |           |         | `set_only_once`. The VM this snapshot is of.                     |
| `server`          | Link → Server                 |      | Y         |         | Denormalized from the VM so the snapshot is locatable without loading it. |
| `status`          | Select                        | Y    | Y         | Pending | `Pending`, `Available`, `Failed`. Set by the controller after the copy Task. |
| `source_image`    | Link → Virtual Machine Image  |      | Y         |         | The image the VM ran when snapshotted (provenance; the clone's kernel comes from it). |
| `disk_gigabytes`  | Int                           |      | Y         |         | Disk size captured, so restore/clone restore the right size.     |
| `size_bytes`      | Long Int                      |      | Y         |         | Actual on-host bytes of the copied rootfs (from the Task output). `Long Int` (bigint) — a 32-bit `Int` overflows on a multi-GB rootfs. |
| `rootfs_path`     | Data                          |      | Y         |         | Absolute on-host path to the snapshot rootfs.                    |

### Form layout

```
── Overview ──
title
virtual_machine
server
| status
── Disk ──
source_image
disk_gigabytes
| size_bytes
  rootfs_path
```

### List view

- Columns: `title`, `virtual_machine`, `status`.
- Standard filters: `virtual_machine`, `status`, `server`.

### Controller methods

- `restore_to_vm()` — restore this snapshot onto its own VM (rollback in
  place). Thin wrapper around `Virtual Machine.rebuild("snapshot", self.name)`
  so the Stopped-state guard lives in one place. Returns the Task name.
- `clone_to_new_vm(title, ssh_public_key, vcpus?, memory_megabytes?,
  disk_gigabytes?)` — create a new VM seeded from this snapshot (fresh
  identity). Disk defaults to the snapshot's size and can only grow.
- `on_trash` — runs [`delete-snapshot-vm.sh`](../scripts/delete-snapshot-vm.sh)
  to delete the on-host files, skipped when the VM is already Terminated
  (its directory is gone).

### Buttons

Shown only on `Available` snapshots:

- **Clone to new VM** (primary) — dialog for new title + SSH key; creates a
  fresh VM and routes to it.
- **Restore to VM** (secondary) — confirm, then `restore_to_vm()`. The VM
  must be Stopped (the underlying `rebuild` enforces it).
- **Delete** (danger) — `confirm_destructive`; deletes the row, which
  cascades the on-host file delete.

---

## Virtual Machine Image

A kernel + rootfs pair, identified by a name.

### Fields

| Field                    | Type   | Reqd | Read-only | Default | Notes                                                |
| ------------------------ | ------ | ---- | --------- | ------- | ---------------------------------------------------- |
| `image_name`             | Data   | Y    |           |         | Primary key. Unique. `set_only_once`. e.g. `ubuntu-24.04`. |
| `title`                  | Data   |      |           |         | Operator-chosen label; `title_field` for the form. `set_only_once`. |
| `is_active`              | Check  |      |           | 1       |                                                      |
| `default_disk_gigabytes` | Int    | Y    |           | 4       | `set_only_once`. Size of the pristine ext4 (per-VM disk grows from this). |
| `kernel_url`             | Data   | Y    |           |         | `set_only_once`. HTTPS URL of the uncompressed `vmlinux`. |
| `kernel_filename`        | Data   | Y    |           |         | `set_only_once`. Filename on the server.             |
| `kernel_sha256`          | Data   | Y    |           |         | `set_only_once`. Hex digest of the kernel.           |
| `rootfs_url`             | Data   | Y    |           |         | `set_only_once`. HTTPS URL of the source squashfs.   |
| `rootfs_filename`        | Data   | Y    |           |         | `set_only_once`. Filename of the resulting ext4 on the server. |
| `rootfs_sha256`          | Data   | Y    |           |         | `set_only_once`. Hex digest of the source squashfs.  |

Every non-`is_active` field is immutable from `after_insert` onward —
the framework `set_only_once` flag paints them read-only on the form,
and the controller's `_validate_immutability` is the
defense-in-depth check.

### Form layout

A single `Overview` Tab Break with the image-data fields under a
collapsible Section Break:

```
image_name
title
| is_active
  default_disk_gigabytes
── Image data ── (collapsible)
kernel_url
kernel_filename
| kernel_sha256
rootfs_url
rootfs_filename
| rootfs_sha256
```

### List view

- Columns (left to right): `name` (the `image_name` autoname),
  `title`, `default_disk_gigabytes`, `is_active`. The legacy
  `image_name` column is dropped from `in_list_view` — the framework
  always renders the autoname as the ID column, so an extra
  `image_name` column was redundant.
- Standard filters: `is_active`.

A first-time operator does not need to invent any of these values. The
Ubuntu 24.04 cloud image constants live in
[`atlas/bootstrap.py`](../atlas/bootstrap.py) as `DEFAULT_IMAGE` (server)
and `MINIMAL_IMAGE` (minimal), mirrored in
[`atlas/tests/e2e/_config.py`](../atlas/tests/e2e/_config.py). Copy one
into the form, or run `atlas.bootstrap.run` which inserts the server row
for you. See [08-images.md](./08-images.md).

### Auto-sync contract

`Virtual Machine Image.after_insert` fans out to every `Server` with
`status = Active`: for each one it calls `self.sync_to_server(server)`,
which enqueues a `sync-image.sh` Task. The operator does *not* press
**Sync to Server** for the initial fan-out — saving the image is the
trigger. Per-attempt tracking happens via the resulting Task rows
(filter the Task list by `script = sync-image.sh`); a dedicated
`Virtual Machine Image Sync` tracking DocType was scoped in the plan
but deferred for the PoC.

The `sync_to_server` and `sync_to_all_servers` whitelisted methods
survive for use by `bootstrap.py` and the e2e harness, but no
operator-facing buttons surface them — the form is effectively
read-only after creation, and the field lock is enforced by both
`set_only_once` and `_validate_immutability`.

### Buttons

- **Archive** (under `Actions ▾`, shown only while `is_active = 1`).
  Calls `archive()` to flip `is_active = 0`. Idempotent.

No primary action, no Sync Status panel, no Sync-to-Server picker on
the form. Initial sync is automatic on save; ad-hoc per-server sync
goes through **Sync Image** on the target Server's `Actions ▾` menu.

---

## Task

One row per shell script execution against a server. Append-only: every field
is read-only on the form. The system writes the row at insert and again when
the run finishes.

### Fields

| Field                   | Type                   | Reqd | Read-only | Default | Notes                                       |
| ----------------------- | ---------------------- | ---- | --------- | ------- | ------------------------------------------- |
| `name`                  | (autoname `hash`)      | Y    | Y         |         | 10-char random hex (Frappe `autoname = "hash"`). |
| `subject`               | Data                   |      | Y         |         | Set in `before_insert` from `SCRIPT_LABELS[script]` (see [04-tasks.md § Task subject](./04-tasks.md#task-subject)). Verb-only when operating on an existing object (`Reboot`, `Start`, `Sync`), verb-noun when creating one (`Bootstrap Server`, `Create Virtual Machine`, `Sync Image`). `title_field` so the form breadcrumb reads it instead of the hash. Indexed. |
| `server`                | Link → Server          |      | Y         |         | Indexed.                                    |
| `virtual_machine`       | Link → Virtual Machine |      | Y         |         | Set when the task is for one VM. Indexed.   |
| `script`                | Data                   | Y    | Y         |         | Path under `atlas/scripts/`, e.g. `provision-vm.sh`. Indexed. |
| `triggered_by`          | Link → User            | Y    | Y         |         | `Administrator` for scheduled jobs.         |
| `status`                | Select                 | Y    | Y         | Pending | `Pending`, `Running`, `Success`, `Failure`. Indexed. |
| `exit_code`             | Int                    |      | Y         |         |                                             |
| `duration_milliseconds` | Int                    |      | Y         |         | Indexed. For sortable list views.           |
| `started`               | Datetime               |      | Y         |         |                                             |
| `ended`                 | Datetime               |      | Y         |         |                                             |
| `variables`             | Long Text (JSON)       | Y    | Y         |         | The env-var dictionary passed to the script.|
| `stdout`                | Code                   |      | Y         |         |                                             |
| `stderr`                | Code                   |      | Y         |         |                                             |

Every operator-visible field is read-only on the form; the table column is
the contract for what the row holds, not for what an operator can type.

`variables` stores the inputs so a task can be replayed by reading the row.
Secrets are not put in `variables`. If a task needs a secret, the secret is
read from another DocType at execution time and not echoed into the Task
record.

### Form layout

A single `Overview` Tab Break with the Output section folded
underneath as a collapsible Section Break (the old `Output` tab is
gone):

```
status
| exit_code
  duration_milliseconds
subject
server
virtual_machine
script
triggered_by
── Timing ──
started
| ended
── Inputs ──
variables
── Output ── (collapsible)
stdout
stderr
```

The client script overlays this with a status-coloured dashboard
headline and a Retry button on Failure. The header `chips` (Server /
VM / Triggered by) and the **Sibling Tasks** quick_list are gone —
both surfaced data already in the form body or the Connections
dashboard. See [10-desk-ui.md § Task](./10-desk-ui.md#task) for the
full behavior.

The controller publishes a `task_update` realtime event (scoped to
the Task's document room) from `after_insert` and `on_update`, with
`{name, status, exit_code, duration_milliseconds, server, virtual_machine, subject}`.
The Task form subscribes and reloads on each tick — long-running
Tasks aren't a black box.

### List view

- Columns (left to right): `subject`, `server`, `virtual_machine`,
  `script`, `status`, `duration_milliseconds`, `started`.
  (Frappe orders list columns by their position in the field schema.
  `started` lives in the Timing section, after the header, so it lands at
  the end of the row. Putting it first would require moving the field
  ahead of the header, which would break the form layout. Operators can
  still sort the list by `started`.)
- Standard filters: `server`, `virtual_machine`, `script`, `status`.
