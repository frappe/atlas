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

| Field             | Type     | Reqd                  | Read-only | Default | Notes                                              |
| ----------------- | -------- | --------------------- | --------- | ------- | -------------------------------------------------- |
| `provider_name`   | Data     | All                   |           |         | Primary key. Unique. e.g. `digitalocean-production`, `home-lab`. |
| `provider_type`   | Select   | All                   |           |         | Options: `DigitalOcean`, `Self-Managed`. `set_only_once`. |
| `is_active`       | Check    |                       |           | 1       |                                                    |
| `api_token`       | Password | `DigitalOcean`        |           |         | DigitalOcean personal access token. Ignored for `Self-Managed`. |
| `ssh_key_id`      | Data     | `DigitalOcean`        |           |         | Fingerprint of the SSH key pre-loaded on droplets. Ignored for `Self-Managed` (no API to register the key with). |
| `ssh_private_key` | Password | All                   |           |         | Matching private key. Atlas uses this to SSH in as `root`. For `Self-Managed`, the public half must already be in the host's `authorized_keys`. |
| `default_region`  | Data     | `DigitalOcean`        |           |         | e.g. `blr1`. Ignored for `Self-Managed`.           |
| `default_size`    | Data     | `DigitalOcean`        |           |         | Must support nested virtualization. Ignored for `Self-Managed`. |
| `default_image`   | Data     | `DigitalOcean`        |           |         | e.g. `ubuntu-24-04-x64`. Ignored for `Self-Managed`. |

The controller's `validate` enforces the table: switching `provider_type`
is forbidden (the field is `set_only_once`); the DO-only fields are
required when `provider_type = DigitalOcean` and otherwise left blank.
Self-Managed rows that accidentally carry a DO field are not rejected —
the field is ignored.

Concrete examples for a fresh `DigitalOcean` row: `default_region = blr1`,
`default_size = s-2vcpu-4gb-intel` (any size that supports nested
virtualisation works), `default_image = ubuntu-24-04-x64`. `ssh_key_id`
is the SHA-256 fingerprint of the SSH key already registered in your DO
account — get it from `doctl compute ssh-key list` or the DO control
panel. `ssh_private_key` is the matching PEM-format private key Atlas
SSHes in with as `root`.

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
ssh_private_key
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

- **Provision Server** (primary) — opens a dialog. The dialog's fields
  depend on `provider_type`:
  - `DigitalOcean`: one field, `server_name`. The dialog shows a
    defaults preview (region, size + estimated monthly USD cost,
    image) sourced from a new whitelisted `preview_cost()` method on
    the provider, then asks for an orange "Create a billable droplet?"
    confirmation before calling the DO API.
  - `Self-Managed`: `server_name`, `ipv4_address`, `ipv6_address`,
    `ipv6_prefix`, `ipv6_virtual_machine_range`. Atlas inserts the
    `Server` directly with the operator-supplied values and runs the
    bootstrap task. No API call. See
    [03-bootstrapping.md](./03-bootstrapping.md).
- **Test Connection** — `DigitalOcean` only; under the `Actions ▾`
  menu. Pings the DO account endpoint. Hidden for `Self-Managed`.

Monthly cost in the preview comes from a hand-maintained
`DIGITALOCEAN_MONTHLY_COST_USD` dict in `server_provider.py` — same
maintenance policy as `default_image`, because DO doesn't expose
per-size pricing in their API. Sizes not in the dict render as "—"
rather than guess.

---

## Server

One row per host. Name is operator-chosen (e.g. `server-blr1-01`).

### Fields

| Field                          | Type                   | Reqd | Read-only | Default | Notes                                                          |
| ------------------------------ | ---------------------- | ---- | --------- | ------- | -------------------------------------------------------------- |
| `server_name`                  | Data                   | Y    |           |         | Primary key. Unique.                                           |
| `provider`                     | Link → Server Provider | Y    |           |         |                                                                |
| `status`                       | Select                 | Y    |           | Pending | `Pending`, `Bootstrapping`, `Active`, `Draining`, `Broken`, `Archived`. |
| `provider_resource_id`         | Data                   |      | Y         |         | DigitalOcean droplet id. Empty for `Self-Managed` providers.   |
| `region`                       | Data                   |      | Y         |         | Copied from provider defaults at insert. Empty for `Self-Managed`. |
| `size`                         | Data                   |      | Y         |         | Copied from provider defaults at insert. Empty for `Self-Managed`. |
| `ipv4_address`                 | Data                   |      | Y         |         | The SSH endpoint. Set by `finish_provisioning` (DigitalOcean) or by the operator at provision time (Self-Managed). |
| `ipv6_address`                 | Data                   |      | Y         |         | The server's own IPv6. Whatever the host actually answers on. |
| `ipv6_prefix`                  | Data                   |      | Y         |         | The full prefix routed to this server (typically a /64). Informational. |
| `ipv6_virtual_machine_range`   | Data                   |      | Y         |         | The subnet Atlas allocates VM addresses from. Any prefix length: `/64`, `/80`, `/124`, ... For `DigitalOcean` this is the /124 derived from the /64 (see [06-networking.md](./06-networking.md)). For `Self-Managed` the operator types it in; it can be the whole extra subnet the host already has routed to it. |
| `architecture`                 | Data                   |      | Y         |         | Set by bootstrap.                                              |
| `firecracker_version`          | Data                   |      | Y         |         | Set by bootstrap.                                              |
| `kernel_version`               | Data                   |      | Y         |         | Set by bootstrap.                                              |
| `notes`                        | Text                   |      |           |         |                                                                |

The split between `ipv6_prefix` and `ipv6_virtual_machine_range` is
because on DigitalOcean a /64 is advertised but only the first /124 is
actually routable; we hand out addresses inside that /124 only. On
Self-Managed hosts the operator might have an entire extra /64 (or /80,
or /48) routed to the box and so the VM range can be much larger than
/124. Atlas treats `ipv6_virtual_machine_range` as "the subnet I am
allowed to allocate from" and does not try to derive it. Details in
[06-networking.md](./06-networking.md).

### Form layout

```
server_name
provider
| status
── Provider resource ──
provider_resource_id
| region
  size
── Networking ──
ipv4_address
ipv6_address
| ipv6_prefix
  ipv6_virtual_machine_range
── Host info ── (collapsible)
architecture
| firecracker_version
  kernel_version
── Notes ── (collapsible)
notes
```

### List view

- Columns (left to right): `server_name`, `provider`, `status`, `region`,
  `ipv4_address`.
- Standard filters: `provider`, `status`, `region`.

### Buttons

- **Bootstrap** (primary on `Pending` / `Bootstrapping` / `Broken`;
  folds under `Actions ▾` as **Re-bootstrap** on `Active`) — runs
  [`scripts/bootstrap-server.sh`](../scripts/bootstrap-server.sh).
  Idempotent.
- **Run Task** (under `Actions ▾`) — opens a script-aware dialog;
  see [04-tasks.md § Run Task](./04-tasks.md#run-task--the-escape-hatch).
- **Reboot** (under `Actions ▾`, danger) — runs
  [`scripts/reboot-server.sh`](../scripts/reboot-server.sh)
  (`systemctl reboot` over SSH). The resulting Task may end in `Failure`
  (SSH drops before the script returns) or `Success` (`systemctl reboot`
  exits before the connection is torn down). Either outcome is normal; the
  meaning is "the server is rebooting." Operators confirm reboot by
  watching for SSH to come back, not by reading the Task status. The
  desk requires the operator to type the server name into a
  text-match dialog before the red button enables — see
  [10-desk-ui.md](./10-desk-ui.md).

Frappe's standard Connections dashboard renders below the form, linking
Virtual Machines and Tasks via their `server` field (configured in
`server_dashboard.py`).

---

## Virtual Machine

One row per microVM. The primary key is a UUID assigned at insert and never
changes — not even on terminate. Predictable, stable identity that survives
deletion.

### Fields

| Field              | Type                          | Reqd | Read-only | Default | Notes                                                            |
| ------------------ | ----------------------------- | ---- | --------- | ------- | ---------------------------------------------------------------- |
| `name`             | UUID                          | Y    | Y         |         | Primary key. Set in `before_insert` via `uuid.uuid4()`.          |
| `description`      | Data                          |      |           |         | Title field; free text (since name is a UUID).                   |
| `server`           | Link → Server                 | Y    |           |         | Immutable after first provision.                                 |
| `image`            | Link → Virtual Machine Image  | Y    |           |         | Immutable.                                                       |
| `status`           | Select                        | Y    | Y         | Pending | `Pending`, `Running`, `Stopped`, `Failed`, `Terminated`. Driven by lifecycle methods only. |
| `vcpus`            | Int                           | Y    |           | 1       | Immutable.                                                       |
| `memory_megabytes` | Int                           | Y    |           | 512     | Immutable.                                                       |
| `disk_gigabytes`   | Int                           | Y    |           | 4       | Immutable.                                                       |
| `ssh_public_key`   | Long Text                     | Y    |           |         | Injected into the rootfs.                                        |
| `ipv6_address`     | Data                          |      | Y         |         | From the server's /124. Set in `before_insert`.                  |
| `mac_address`      | Data                          |      | Y         |         | Derived from `name`. Set in `before_validate`.                   |
| `tap_device`       | Data                          |      | Y         |         | Derived from `name`. Set in `before_validate`.                   |
| `last_started`     | Datetime                      |      | Y         |         |                                                                  |
| `last_stopped`     | Datetime                      |      | Y         |         |                                                                  |

Because the name is a UUID, the operator needs `description` to recognize a
VM in lists. Optional but recommended; it's the form's title field.

`status` is read-only on the form because it is only ever set by lifecycle
methods (Provision/Start/Stop/Restart/Terminate); see
[05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md).

`ssh_public_key` is the key injected into the *guest's*
`/root/.ssh/authorized_keys` — it is how the operator SSHes into the
VM, not into the host. The host key lives on the `Server Provider`.

### Form layout

```
description
server
image
| status
── Resources ──
vcpus
| memory_megabytes
| disk_gigabytes
── Access ──
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

- Columns (left to right): `description`, `server`, `image`, `status`,
  `ipv6_address`.
- Standard filters: `server`, `image`, `status`.

### Buttons

Tiering is keyed off `status` — see [10-desk-ui.md § Virtual Machine](./10-desk-ui.md#virtual-machine):

- **Provision** (primary on `Pending` / `Failed`) — runs
  [`scripts/provision-vm.sh`](../scripts/provision-vm.sh).
- **Start** (primary on `Stopped`) — `Stopped` → `Running`.
- **Stop** (primary on `Running`) — `Running` → `Stopped`.
- **Restart** (secondary on `Stopped` / `Running`) → `Running`.
- **Terminate** (under `Actions ▾`, danger; available until
  `Terminated`) — runs
  [`scripts/terminate-vm.sh`](../scripts/terminate-vm.sh), sets
  `status = Terminated`. The UUID does not change. The desk requires
  the operator to type the VM's 8-char short ID into a text-match
  dialog before the red button enables.

---

## Virtual Machine Image

A kernel + rootfs pair, identified by a name.

### Fields

| Field                    | Type   | Reqd | Read-only | Default | Notes                                                |
| ------------------------ | ------ | ---- | --------- | ------- | ---------------------------------------------------- |
| `image_name`             | Data   | Y    |           |         | Primary key. Unique. `set_only_once`. e.g. `ubuntu-24.04`. |
| `description`            | Data   |      |           |         |                                                      |
| `is_active`              | Check  |      |           | 1       |                                                      |
| `default_disk_gigabytes` | Int    | Y    |           | 4       | Size of the pristine ext4 (per-VM disk grows from this). |
| `kernel_url`             | Data   | Y    |           |         | HTTPS URL of the uncompressed `vmlinux`.             |
| `kernel_filename`        | Data   | Y    |           |         | Filename on the server.                              |
| `kernel_sha256`          | Data   | Y    |           |         | Hex digest of the kernel.                            |
| `rootfs_url`             | Data   | Y    |           |         | HTTPS URL of the source squashfs.                    |
| `rootfs_filename`        | Data   | Y    |           |         | Filename of the resulting ext4 on the server.        |
| `rootfs_sha256`          | Data   | Y    |           |         | Hex digest of the source squashfs.                   |

### Form layout

```
image_name
description
| is_active
  default_disk_gigabytes
── Kernel ──
kernel_url
kernel_filename
| kernel_sha256
── Rootfs ──
rootfs_url
rootfs_filename
| rootfs_sha256
```

### List view

- Columns (left to right): `image_name`, `description`,
  `default_disk_gigabytes`, `is_active`.
- Standard filters: `is_active`.

A first-time operator does not need to invent any of these values. The
Firecracker CI Ubuntu 24.04 image constants live in
[`atlas/bootstrap.py`](../atlas/bootstrap.py) as `DEFAULT_IMAGE` and in
[`atlas/tests/e2e/_config.py`](../atlas/tests/e2e/_config.py) as
`DEFAULT_IMAGE`. Copy them into the form, or run `atlas.bootstrap.run`
which inserts the row for you. See [08-images.md](./08-images.md).

### Buttons

- **Sync to Server** (secondary) — runs
  [`scripts/sync-image.sh`](../scripts/sync-image.sh) on a single
  server. The picker uses `only_select: 1` (no "+ Create" affordance)
  and a `status = Active` filter — see
  [10-desk-ui.md § Virtual Machine Image](./10-desk-ui.md#virtual-machine-image).
- **Sync to All Servers** (under `Actions ▾`) — same, against every
  active server. Before fanning out the desk shows an orange
  confirmation listing the active targets.

---

## Task

One row per shell script execution against a server. Append-only: every field
is read-only on the form. The system writes the row at insert and again when
the run finishes.

### Fields

| Field                   | Type                   | Reqd | Read-only | Default | Notes                                       |
| ----------------------- | ---------------------- | ---- | --------- | ------- | ------------------------------------------- |
| `name`                  | (autoname `hash`)      | Y    | Y         |         | 10-char random hex (Frappe `autoname = "hash"`). |
| `subject`               | Data                   |      | Y         |         | Set in `before_insert` from `(script, virtual_machine, server)` — e.g. `Provision VM · verify vnet_hdr fix on bootstrap-server-…`. `title_field` so the form breadcrumb reads it instead of the hash. Indexed. |
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

```
subject
server
virtual_machine
script
triggered_by
| status
  exit_code
  duration_milliseconds
── Timing ──
started
| ended
── Inputs ──
variables
── Output ──
stdout
stderr
```

The client script overlays this with a status-coloured dashboard
headline, related-record chips, sibling-tasks list, and a Retry button
on Failure. See [10-desk-ui.md § Task](./10-desk-ui.md#task) for the
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
