# DocTypes

Five DocTypes. Module `Atlas`. None are submittable. All track changes. Read
permission for `System Manager`.

1. [Server Provider](#server-provider)
2. [Server](#server)
3. [Virtual Machine](#virtual-machine)
4. [Virtual Machine Image](#virtual-machine-image)
5. [Task](#task)

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

## Server Provider

One row per source of servers. Two provider types are implemented:

- `DigitalOcean` — Atlas calls the DO API to create the droplet.
- `Self-Managed` — the operator brings their own host. Atlas does not
  create or destroy anything; the provider only carries the SSH
  credentials.

The required-ness of every field below depends on `provider_type`. The
"Reqd" column lists which types require it.

### Fields

| Field                  | Type     | Reqd                  | Read-only | Default | Notes                                              |
| ---------------------- | -------- | --------------------- | --------- | ------- | -------------------------------------------------- |
| `provider_name`        | Data     | All                   |           |         | Primary key. Unique. `set_only_once`. e.g. `digitalocean-production`, `home-lab`. |
| `provider_type`        | Select   | All                   |           |         | Options: `DigitalOcean`, `Self-Managed`. `set_only_once`. |
| `is_active`            | Check    |                       |           | 1       | `set_only_once`. Flip via the `archive()` controller method, not the form. |
| `api_token`            | Password | `DigitalOcean`        |           |         | `set_only_once`. DigitalOcean personal access token. Ignored for `Self-Managed`. |
| `ssh_key_id`           | Data     | `DigitalOcean`        |           |         | `set_only_once`. Fingerprint of the SSH key pre-loaded on droplets. Ignored for `Self-Managed` (no API to register the key with). |
| `ssh_private_key_path` | Data     | All                   |           |         | `set_only_once`. Absolute path on the Atlas host where the SSH private key lives. Atlas reads the PEM at SSH-connect time via `secrets.get_ssh_key_from_disk(path)`. Keep the file `0600` owned by the Frappe user. |
| `default_region`       | Data     | `DigitalOcean`        |           |         | `set_only_once`. e.g. `blr1`. Ignored for `Self-Managed`. |
| `default_size`         | Data     | `DigitalOcean`        |           |         | `set_only_once`. Must support nested virtualization. Ignored for `Self-Managed`. |
| `default_image`        | Data     | `DigitalOcean`        |           |         | `set_only_once`. e.g. `ubuntu-24-04-x64`. Ignored for `Self-Managed`. |

The controller's `validate` enforces the table: switching `provider_type`
is forbidden (the field is `set_only_once`); the DO-only fields are
required when `provider_type = DigitalOcean` and otherwise left blank.
Self-Managed rows that accidentally carry a DO field are not rejected —
the field is ignored. Every Auth + Defaults field carries
`set_only_once`, so the form paints them read-only after first save.
A defense-in-depth `_validate_immutability` in the controller also
raises if a `frappe.db.set_value`-style backdoor mutation ever sneaks
in.

Concrete examples for a fresh `DigitalOcean` row: `default_region = blr1`,
`default_size = s-2vcpu-4gb-intel` (any size that supports nested
virtualisation works), `default_image = ubuntu-24-04-x64`. `ssh_key_id`
is the SHA-256 fingerprint of the SSH key already registered in your DO
account — get it from `doctl compute ssh-key list` or the DO control
panel. `ssh_private_key_path` points at a `0600` PEM on disk —
typically `/etc/atlas/keys/<provider_name>.pem`. Atlas reads it once
per SSH connection; rotating the key is a file-replace operation, no
DocType edit.

For `Self-Managed`, all four networking inputs to the **Provision
Server** dialog are operator-supplied: `ipv4_address` is the SSH
endpoint, `ipv6_address` is whatever the host answers on, `ipv6_prefix`
is the full prefix routed to the host (typically `/64`), and
`ipv6_virtual_machine_range` is the subnet Atlas is allowed to allocate
VM addresses from. The split between the latter two is explained in the
`Server` section below.

### Form layout

```
provider_name
provider_type
| is_active
── Authentication ──
api_token
ssh_key_id
ssh_private_key_path
── Defaults for new servers ──
default_region
| default_size
  default_image
```

The DigitalOcean-only sections stay on the form for both types but the
fields inside them are non-required for `Self-Managed`. (Hiding them
conditionally is a desk-only nicety; the spec does not require it.)

### List view

- Columns (left to right): `provider_name`, `provider_type`, `is_active`,
  `default_region`.
- Standard filters: `provider_type`, `is_active`.

### Buttons

- **Provision Server** (primary) — opens a dialog. The dialog asks for
  a `title` (the user-facing label; lowercase + digits + hyphens, max 63
  chars). The Server row's `name` is assigned a UUID at insert; the
  title is passed through to the DigitalOcean droplet `name` and tag.
  Remaining fields depend on `provider_type`:
  - `DigitalOcean`: three editable `Select` fields (`region`, `size`,
    `image`) defaulting to the provider's `default_*`, sourced from
    `atlas.atlas.api.provider_options.provider_options`. Then asks for
    an orange "Create a billable droplet?" confirmation before calling
    the DO API.
  - `Self-Managed`: `title`, `ipv4_address`, `ipv6_address`,
    `ipv6_prefix`, `ipv6_virtual_machine_range`. Atlas inserts the
    `Server` directly with the operator-supplied values and runs the
    bootstrap task. No API call. See
    [03-bootstrapping.md](./03-bootstrapping.md).

  The whitelisted `provision_server(title, ...)` controller method
  returns the new row's UUID `name` (so the client can route to the
  form), not the title.
- **Test Connection** — `DigitalOcean` only; under the `Actions ▾`
  menu. Pings the DO account endpoint. Hidden for `Self-Managed`.
- **Archive** — `Actions ▾` menu, shown only when `is_active = 1`.
  Calls the whitelisted `archive()` controller method, which flips
  `is_active = 0` via `db.set_value` (bypassing `set_only_once`).
  Existing Servers keep their FK reference so historical Tasks stay
  queryable; a full decommission flow (cascade across child Servers,
  cost-center accounting) is a follow-up plan, out of scope here.

Monthly cost in the preview comes from a hand-maintained
`DIGITALOCEAN_MONTHLY_COST_USD` dict in `server_provider.py` — same
maintenance policy as `default_image`, because DO doesn't expose
per-size pricing in their API. Sizes not in the dict render as "—"
rather than guess.

---

## Server

One row per host. The primary key is a UUID assigned at insert; the
operator-facing label lives in `title` (e.g. `server-blr1-01`).

### Fields

| Field                          | Type                   | Reqd | Read-only | Default | Notes                                                          |
| ------------------------------ | ---------------------- | ---- | --------- | ------- | -------------------------------------------------------------- |
| `name`                         | UUID (autoname)        | Y    | Y         |         | Primary key. UUID minted in `Server.autoname()`. Stable for the row's lifetime; no rename UI. |
| `title`                        | Data                   | Y    |           |         | Operator-chosen label. `set_only_once` — first save freezes it. |
| `provider`                     | Link → Server Provider | Y    |           |         | `set_only_once`. |
| `status`                       | Select                 | Y    | Y         | Pending | `Pending`, `Bootstrapping`, `Active`, `Draining`, `Broken`, `Archived`. Controllers mutate via `db.set_value`. |
| `provider_resource_id`         | Data                   |      | Y         |         | DigitalOcean droplet id. Empty for `Self-Managed` providers. Locked once written by the controller. |
| `region`                       | Data                   |      | Y         |         | Copied from provider defaults at insert. Empty for `Self-Managed`. |
| `size`                         | Data                   |      | Y         |         | Copied from provider defaults at insert. Empty for `Self-Managed`. |
| `ipv4_address`                 | Data                   |      | Y         |         | The SSH endpoint. Set by `finish_provisioning` (DigitalOcean) or by the operator at provision time (Self-Managed). Locked once written. |
| `ipv6_address`                 | Data                   |      | Y         |         | The server's own IPv6. Whatever the host actually answers on. |
| `ipv6_prefix`                  | Data                   |      | Y         |         | The full prefix routed to this server (typically a /64). Informational. |
| `ipv6_virtual_machine_range`   | Data                   |      | Y         |         | The subnet Atlas allocates VM addresses from. Any prefix length: `/64`, `/80`, `/124`, ... For `DigitalOcean` this is the /124 derived from the /64 (see [06-networking.md](./06-networking.md)). For `Self-Managed` the operator types it in; it can be the whole extra subnet the host already has routed to it. |
| `architecture`                 | Data                   |      | Y         |         | Set by bootstrap. Allowed to change on re-bootstrap. |
| `firecracker_version`          | Data                   |      | Y         |         | Set by bootstrap. Allowed to change on re-bootstrap. |
| `kernel_version`               | Data                   |      | Y         |         | Set by bootstrap. Allowed to change on re-bootstrap. |
| `notes`                        | Text                   |      |           |         |                                                                |

Immutability is enforced by `Server._validate_immutability()` (lock
once a value is written; allow `None → value` for fields the
DigitalOcean provision flow populates lazily, like IPv4/6). The
framework `set_only_once` flag covers `title` and `provider` because
those are populated at insert time and never legitimately change.

### Controller methods

- `archive()` — sets `status = "Archived"`. Idempotent (rejects if
  already Archived). Existing FKs from Virtual Machine and Task rows
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
| region
  size
── Networking (collapsible) ──
ipv4_address
ipv6_address
| ipv6_prefix
  ipv6_virtual_machine_range
── Host info (collapsible) ──
architecture
| firecracker_version
  kernel_version
── Notes (collapsible) ──
notes
```

### List view

- Columns (left to right): `title`, `provider`, `status`, `region`,
  `ipv4_address`.
- Standard filters: `provider`, `status`, `region`.

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
- **Archive** (under `Actions ▾`, on non-`Archived` rows) — confirms via
  type-the-title dialog, then sets `status = "Archived"`.
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
| `image`            | Link → Virtual Machine Image  | Y    |           |         | Immutable after insert (via `_validate_immutability`).           |
| `status`           | Select                        | Y    | Y         | Pending | `Pending`, `Running`, `Stopped`, `Failed`, `Terminated`. Driven by lifecycle methods only. |
| `vcpus`            | Int                           | Y    |           | 1       | Immutable after insert.                                          |
| `memory_megabytes` | Int                           | Y    |           | 512     | Immutable after insert.                                          |
| `disk_gigabytes`   | Int                           | Y    |           | 4       | Immutable after insert.                                          |
| `ssh_public_key`   | Long Text                     | Y    |           |         | `set_only_once`. Injected into the rootfs.                       |
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
VM, not into the host. The host key lives on the `Server Provider`.

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
- **Stop** (primary on `Running`) — `Running` → `Stopped`.
- **Restart** (secondary on `Stopped` / `Running`) → `Running`.
- **Terminate** (under `Actions ▾`, danger; available until
  `Terminated`) — runs
  [`scripts/terminate-vm.sh`](../scripts/terminate-vm.sh), sets
  `status = Terminated`. The UUID does not change. The desk requires
  the operator to type the VM's `title` into a `confirm_destructive`
  dialog before the red button enables; the dialog body is empty —
  typing the title is the entire deterrent.

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
Firecracker CI Ubuntu 24.04 image constants live in
[`atlas/bootstrap.py`](../atlas/bootstrap.py) as `DEFAULT_IMAGE` and in
[`atlas/tests/e2e/_config.py`](../atlas/tests/e2e/_config.py) as
`DEFAULT_IMAGE`. Copy them into the form, or run `atlas.bootstrap.run`
which inserts the row for you. See [08-images.md](./08-images.md).

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
