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

One row per cloud account. Only `DigitalOcean` is implemented in this
iteration.

### Fields

| Field             | Type     | Reqd | Read-only | Default | Notes                                              |
| ----------------- | -------- | ---- | --------- | ------- | -------------------------------------------------- |
| `provider_name`   | Data     | Y    |           |         | Primary key. Unique. e.g. `digitalocean-production`. |
| `provider_type`   | Select   | Y    |           |         | Options: `DigitalOcean`.                           |
| `is_active`       | Check    |      |           | 1       |                                                    |
| `api_token`       | Password | Y    |           |         | DigitalOcean personal access token.                |
| `ssh_key_id`      | Data     | Y    |           |         | Fingerprint of the SSH key pre-loaded on droplets. |
| `ssh_private_key` | Password | Y    |           |         | Matching private key. Atlas uses this to SSH in.   |
| `default_region`  | Data     | Y    |           |         | e.g. `blr1`.                                       |
| `default_size`    | Data     | Y    |           |         | Must support nested virtualization.                |
| `default_image`   | Data     | Y    |           |         | e.g. `ubuntu-24-04-x64`.                           |

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

### List view

- Columns (left to right): `provider_name`, `provider_type`, `is_active`,
  `default_region`.
- Standard filters: `provider_type`, `is_active`.

### Buttons

- **Provision Server** — opens a dialog asking for a `server_name`; creates a
  droplet, inserts a `Server`, runs the bootstrap task.
- **Test Connection** — pings the DigitalOcean account endpoint.

---

## Server

One row per host. Name is operator-chosen (e.g. `server-blr1-01`).

### Fields

| Field                          | Type                   | Reqd | Read-only | Default | Notes                                                          |
| ------------------------------ | ---------------------- | ---- | --------- | ------- | -------------------------------------------------------------- |
| `server_name`                  | Data                   | Y    |           |         | Primary key. Unique.                                           |
| `provider`                     | Link → Server Provider | Y    |           |         |                                                                |
| `status`                       | Select                 | Y    |           | Pending | `Pending`, `Bootstrapping`, `Active`, `Draining`, `Broken`, `Archived`. |
| `provider_resource_id`         | Data                   |      | Y         |         | DigitalOcean droplet id.                                       |
| `region`                       | Data                   |      | Y         |         | Copied from provider defaults at insert.                       |
| `size`                         | Data                   |      | Y         |         | Copied from provider defaults at insert.                       |
| `ipv4_address`                 | Data                   |      | Y         |         | The SSH endpoint. Set by `finish_provisioning`.                |
| `ipv6_address`                 | Data                   |      | Y         |         | The server's own IPv6 (typically `::1` of /64; whatever DO assigns). |
| `ipv6_prefix`                  | Data                   |      | Y         |         | The /64 routed to this server.                                 |
| `ipv6_virtual_machine_range`   | Data                   |      | Y         |         | The /124 carved from the /64 we hand out from.                 |
| `architecture`                 | Data                   |      | Y         |         | Set by bootstrap.                                              |
| `firecracker_version`          | Data                   |      | Y         |         | Set by bootstrap.                                              |
| `kernel_version`               | Data                   |      | Y         |         | Set by bootstrap.                                              |
| `notes`                        | Text                   |      |           |         |                                                                |

The split between `ipv6_prefix` (/64) and `ipv6_virtual_machine_range` (/124)
is because DigitalOcean assigns a /64 but only the first /124 is actually
routable inside DO's fabric. We hand out addresses inside the /124 only.
Details in [06-networking.md](./06-networking.md).

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

- **Bootstrap** — runs [`scripts/bootstrap-server.sh`](../scripts/bootstrap-server.sh).
  Idempotent.
- **Run Task** — opens a dialog with a script picker + variables. Runs as a
  Task.
- **Reboot** — runs [`scripts/reboot-server.sh`](../scripts/reboot-server.sh)
  (`systemctl reboot` over SSH). The resulting Task may end in `Failure`
  (SSH drops before the script returns) or `Success` (`systemctl reboot`
  exits before the connection is torn down). Either outcome is normal; the
  meaning is "the server is rebooting." Operators confirm reboot by
  watching for SSH to come back, not by reading the Task status.

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

- **Provision** — only enabled when `status` is `Pending` or `Failed`. Runs
  [`scripts/provision-vm.sh`](../scripts/provision-vm.sh).
- **Start** — `Stopped` → `Running`.
- **Stop** — `Running` → `Stopped`.
- **Restart** — `Stopped`/`Running` → `Running`.
- **Terminate** — runs [`scripts/terminate-vm.sh`](../scripts/terminate-vm.sh),
  sets `status = Terminated`. The UUID does not change.

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

### Buttons

- **Sync to All Servers** — runs [`scripts/sync-image.sh`](../scripts/sync-image.sh)
  against every active server.
- **Sync to Server** — same, for a single server.

---

## Task

One row per shell script execution against a server. Append-only: every field
is read-only on the form. The system writes the row at insert and again when
the run finishes.

### Fields

| Field                   | Type                   | Reqd | Read-only | Default | Notes                                       |
| ----------------------- | ---------------------- | ---- | --------- | ------- | ------------------------------------------- |
| `name`                  | (autoname `hash`)      | Y    | Y         |         | 10-char random hex (Frappe `autoname = "hash"`). |
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

### List view

- Columns (left to right): `server`, `virtual_machine`, `script`, `status`,
  `duration_milliseconds`, `started`.
  (Frappe orders list columns by their position in the field schema.
  `started` lives in the Timing section, after the header, so it lands at
  the end of the row. Putting it first would require moving the field
  ahead of the header, which would break the form layout. Operators can
  still sort the list by `started`.)
- Standard filters: `server`, `virtual_machine`, `script`, `status`.
