# DocTypes

Five DocTypes. Module `Atlas`. None are submittable. All track changes. Read
permission for `System Manager`.

1. [Server Provider](#server-provider)
2. [Server](#server)
3. [Virtual Machine](#virtual-machine)
4. [Virtual Machine Image](#virtual-machine-image)
5. [Task](#task)

---

## Server Provider

One row per cloud account. Only `DigitalOcean` is implemented in this
iteration.

| Field             | Type                 | Reqd | Notes                                              |
| ----------------- | -------------------- | ---- | -------------------------------------------------- |
| `provider_name`   | Data                 | Y    | Primary key. e.g. `digitalocean-production`.       |
| `provider_type`   | Select               | Y    | Options: `DigitalOcean`.                           |
| `api_token`       | Password             | Y    | DigitalOcean personal access token.                |
| `default_region`  | Data                 | Y    | e.g. `blr1`.                                       |
| `default_size`    | Data                 | Y    | Must support nested virtualization.                |
| `default_image`   | Data                 | Y    | e.g. `ubuntu-24-04-x64`.                           |
| `ssh_key_id`      | Data                 | Y    | Fingerprint of the SSH key pre-loaded on droplets. |
| `ssh_private_key` | Password (Long Text) | Y    | Matching private key. Atlas uses this to SSH in.   |
| `is_active`       | Check                |      | Defaults to 1.                                     |

Buttons:

- **Provision Server** — opens a dialog asking for a `server_name`; creates a
  droplet, inserts a `Server`, runs the bootstrap task.
- **Test Connection** — pings the DigitalOcean account endpoint.

### Server Provider form wireframe

```
+-----------------------------------------------------------+
| Server Provider: digitalocean-production       [Active]   |
+-----------------------------------------------------------+
|  Provider Name *      [ digitalocean-production         ] |
|  Provider Type *      [ DigitalOcean                  v ] |
|  API Token *          [ ************************        ] |
|                                                           |
|  Defaults                                                 |
|  Default Region *     [ blr1                            ] |
|  Default Size *       [ s-2vcpu-4gb-intel               ] |
|  Default Image *      [ ubuntu-24-04-x64                ] |
|  SSH Key ID *         [ 12:34:56:...:ab                 ] |
|  SSH Private Key *    [ -----BEGIN OPENSSH PRIVATE KEY- ] |
|                                                           |
|  [x] Is Active                                            |
|                                                           |
|  [ Test Connection ]    [ Provision Server ]             |
+-----------------------------------------------------------+
```

---

## Server

One row per host. Name is operator-chosen (e.g. `server-blr1-01`).

| Field                  | Type                          | Reqd | Notes                                          |
| ---------------------- | ----------------------------- | ---- | ---------------------------------------------- |
| `server_name`          | Data                          | Y    | Primary key.                                   |
| `provider`             | Link → Server Provider        | Y    |                                                |
| `provider_resource_id` | Data                          | Y    | DigitalOcean droplet id. Read-only after set.  |
| `region`               | Data                          | Y    | Read-only.                                     |
| `size`                 | Data                          | Y    | Read-only.                                     |
| `ipv4_address`         | Data                          | Y    | The SSH endpoint for Atlas.                    |
| `ipv6_address`         | Data                          | Y    | The server's own IPv6 (typically `::1` of /64; whatever DO assigns). |
| `ipv6_prefix`          | Data                          | Y    | The /64 routed to this server.                 |
| `ipv6_virtual_machine_range` | Data                    | Y    | The /124 carved from the /64 we hand out from. |
| `status`               | Select                        | Y    | `Pending`, `Bootstrapping`, `Active`, `Draining`, `Broken`, `Archived`. |
| `architecture`         | Data                          |      | Set by bootstrap.                              |
| `firecracker_version`  | Data                          |      | Set by bootstrap.                              |
| `kernel_version`       | Data                          |      | Set by bootstrap.                              |
| `notes`                | Text                          |      |                                                |

The split between `ipv6_prefix` (/64) and `ipv6_virtual_machine_range` (/124)
is because DigitalOcean assigns a /64 but only the first /124 is actually
routable inside DO's fabric. We hand out addresses inside the /124 only.
Details in [06-networking.md](./06-networking.md).

Buttons:

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

### Server form wireframe

```
+-----------------------------------------------------------------+
| Server: server-blr1-01                          [Active]    [v] |
+-----------------------------------------------------------------+
|  Server Name *         [ server-blr1-01                      ]  |
|  Provider *            [ digitalocean-production       v ]      |
|  Provider Resource ID  [ 412345678              ] (read-only)   |
|  Region                [ blr1                   ] (read-only)   |
|  Size                  [ s-2vcpu-4gb-intel      ] (read-only)   |
|                                                                 |
|  Networking                                                     |
|  IPv4 Address *        [ 139.59.x.y                          ]  |
|  IPv6 Address *        [ 2a03:b0c0:abcd:1234::1              ]  |
|  IPv6 Prefix *         [ 2a03:b0c0:abcd:1234::/64            ]  |
|  IPv6 VM Range *       [ 2a03:b0c0:abcd:1234::/124           ]  |
|                                                                 |
|  State                                                          |
|  Status *              [ Active                          v ]    |
|  Architecture          [ x86_64                 ]               |
|  Firecracker Version   [ 1.15.1                 ]               |
|  Kernel Version        [ 6.8.0-31-generic       ]               |
|                                                                 |
|  Notes                                                          |
|  [                                                           ]  |
|                                                                 |
|  [ Bootstrap ]  [ Run Task ]  [ Reboot ]                        |
|                                                                 |
|  ── Virtual Machines on this server ───────────────────────     |
|  d4f7...   Running   2 vCPU  2048 MB  2a03:b0c0:abcd:1234::2    |
|  19ae...   Stopped   1 vCPU  1024 MB  2a03:b0c0:abcd:1234::3    |
|                                                                 |
|  ── Recent Tasks ─────────────────────────────────────────      |
|  2026-05-25 13:01  bootstrap-server.sh     Success    12.3s     |
|  2026-05-25 13:11  provision-vm.sh         Success     3.4s     |
+-----------------------------------------------------------------+
```

---

## Virtual Machine

One row per microVM. The primary key is a UUID assigned at insert and never
changes — not even on archive. This is the change from the previous draft:
predictable, stable identity that survives deletion.

| Field                  | Type                          | Reqd | Notes                                                   |
| ---------------------- | ----------------------------- | ---- | ------------------------------------------------------- |
| `name`                 | UUID                          | Y    | Primary key. Set in `before_insert` via `uuid.uuid4()`. |
| `server`               | Link → Server                 | Y    | Immutable after first provision.                        |
| `image`                | Link → Virtual Machine Image  | Y    | Immutable.                                              |
| `vcpus`                | Int                           | Y    | Defaults to 1. Immutable.                               |
| `memory_megabytes`     | Int                           | Y    | Defaults to 512. Immutable.                             |
| `disk_gigabytes`       | Int                           | Y    | Defaults to 4. Immutable.                               |
| `ipv6_address`         | Data                          | Y    | From the server's /124.                                 |
| `mac_address`          | Data                          | Y    | Derived from `name`.                                    |
| `tap_device`           | Data                          | Y    | Derived from `name`.                                    |
| `ssh_public_key`       | Long Text                     | Y    | Injected into the rootfs.                               |
| `status`               | Select                        | Y    | `Pending`, `Provisioning`, `Running`, `Stopped`, `Failed`, `Archived`. |
| `last_started`         | Datetime                      |      |                                                         |
| `last_stopped`         | Datetime                      |      |                                                         |
| `description`          | Data                          |      | Free text (since name is a UUID).                       |

Because the name is a UUID, the operator needs a `description` to recognize
a VM in lists. Optional but recommended.

Buttons:

- **Provision** — only enabled when `status` is `Pending` or `Failed`. Runs
  [`scripts/provision-vm.sh`](../scripts/provision-vm.sh).
- **Start** — `Stopped` → `Running`.
- **Stop** — `Running` → `Stopped`.
- **Restart** — `Stopped`/`Running` → `Running`.
- **Delete** — runs [`scripts/delete-vm.sh`](../scripts/delete-vm.sh), sets
  `status = Archived`. The UUID does not change.

### Virtual Machine form wireframe

```
+-----------------------------------------------------------------+
| Virtual Machine: d4f7c1a2-...-9b3e            [Running]     [v] |
+-----------------------------------------------------------------+
|  Name              d4f7c1a2-7e0a-4f1b-93cc-ad96b9b39b3e         |
|  Description       [ first try, blr1                         ]  |
|  Server *          [ server-blr1-01                    v ]      |
|  Image *           [ ubuntu-24.04                      v ]      |
|                                                                 |
|  Resources                                                      |
|  vCPUs *             [ 2     ]                                  |
|  Memory (MB) *       [ 2048  ]                                  |
|  Disk (GB) *         [ 4     ]                                  |
|                                                                 |
|  Networking                                                     |
|  IPv6 Address *      [ 2a03:b0c0:abcd:1234::2               ]   |
|  MAC Address *       [ 06:00:d4:f7:c1:a2                    ]   |
|  TAP Device *        [ atlas-d4f7c1a27e               ]         |
|                                                                 |
|  Access                                                         |
|  SSH Public Key *    [ ssh-ed25519 AAAA... user@host        ]   |
|                                                                 |
|  State                                                          |
|  Status *            [ Running                           v ]    |
|  Last Started        [ 2026-05-25 13:11:02                  ]   |
|  Last Stopped        [                                      ]   |
|                                                                 |
|  [ Provision ]  [ Start ]  [ Stop ]  [ Restart ]  [ Delete ]    |
|                                                                 |
|  ── Recent Tasks ─────────────────────────────────────────      |
|  2026-05-25 13:11  provision-vm.sh     Success   3.4s           |
|  2026-05-25 13:14  stop-vm.sh          Success   0.3s           |
+-----------------------------------------------------------------+
```

---

## Virtual Machine Image

A kernel + rootfs pair, identified by a name.

| Field                  | Type   | Reqd | Notes                                                |
| ---------------------- | ------ | ---- | ---------------------------------------------------- |
| `image_name`           | Data   | Y    | Primary key. e.g. `ubuntu-24.04`.                    |
| `description`          | Data   |      |                                                      |
| `kernel_url`           | Data   | Y    | HTTPS URL of the uncompressed `vmlinux`.             |
| `kernel_filename`      | Data   | Y    | Filename on the server.                              |
| `kernel_sha256`        | Data   | Y    | Hex digest of the kernel.                            |
| `rootfs_url`           | Data   | Y    | HTTPS URL of the source squashfs.                    |
| `rootfs_filename`      | Data   | Y    | Filename of the resulting ext4 on the server.        |
| `rootfs_sha256`        | Data   | Y    | Hex digest of the source squashfs.                   |
| `default_disk_gigabytes` | Int  | Y    | Size of the pristine ext4 (per-VM disk grows from this). |
| `is_active`            | Check  |      | Defaults to 1.                                       |

Buttons:

- **Sync to All Servers** — run [`scripts/sync-image.sh`](../scripts/sync-image.sh)
  against every active server.
- **Sync to Server** — same, for a single server.

### Virtual Machine Image form wireframe

```
+-----------------------------------------------------------------+
| Virtual Machine Image: ubuntu-24.04             [Active]    [v] |
+-----------------------------------------------------------------+
|  Image Name *        [ ubuntu-24.04                          ]  |
|  Description         [ Firecracker CI Ubuntu 24.04 rootfs    ]  |
|                                                                 |
|  Kernel                                                         |
|  Kernel URL *        [ https://s3.amazonaws.com/.../vmlinux- ]  |
|  Kernel Filename *   [ vmlinux-6.1.141                       ]  |
|  Kernel SHA-256 *    [ a3f9...                               ]  |
|                                                                 |
|  Rootfs                                                         |
|  Rootfs URL *        [ https://s3.amazonaws.com/.../ubuntu-2 ]  |
|  Rootfs Filename *   [ ubuntu-24.04.ext4                     ]  |
|  Rootfs SHA-256 *    [ 7b21...                               ]  |
|  Default Disk (GB) * [ 4                                     ]  |
|                                                                 |
|  [x] Is Active                                                  |
|                                                                 |
|  [ Sync to All Servers ]                                        |
+-----------------------------------------------------------------+
```

---

## Task

One row per shell script execution against a server. Append-only.

| Field                 | Type                   | Reqd | Notes                                       |
| --------------------- | ---------------------- | ---- | ------------------------------------------- |
| `name`                | (autoname `hash`)      | Y    | 10-char random hex (Frappe `autoname = "hash"`). |
| `server`              | Link → Server          | Y    |                                             |
| `virtual_machine`     | Link → Virtual Machine |      | Set when the task is for one VM.            |
| `script`              | Data                   | Y    | Path under `atlas/scripts/`, e.g. `provision-vm.sh`. |
| `variables`           | Long Text (JSON)       | Y    | The env-var dictionary passed to the script.|
| `status`              | Select                 | Y    | `Pending`, `Running`, `Success`, `Failure`. |
| `exit_code`           | Int                    |      |                                             |
| `stdout`              | Code                   |      |                                             |
| `stderr`              | Code                   |      |                                             |
| `started`             | Datetime               |      |                                             |
| `ended`               | Datetime               |      |                                             |
| `duration_milliseconds` | Int                  |      | For sortable list views.                    |
| `triggered_by`        | Link → User            | Y    | `Administrator` for scheduled jobs.         |

Read-only after insert. Indexed: `server`, `virtual_machine`, `status`, `script`.

`variables` stores the inputs so a task can be replayed by reading the row.
Secrets are not put in `variables`. If a task needs a secret, the secret is
read from another DocType at execution time and not echoed into the Task
record.

### Task list wireframe

```
+-----------------------------------------------------------------+
| Tasks                                                           |
+-----------------------------------------------------------------+
|  Server          VM        Script             Status   Dur      |
|  server-blr1-01  d4f7...   provision-vm.sh    Success   3.4s    |
|  server-blr1-01  —         bootstrap-server.. Success  12.3s    |
|  server-blr1-01  d4f7...   stop-vm.sh         Success   0.3s    |
|  server-blr1-02  19ae...   provision-vm.sh    Failure   2.1s    |
+-----------------------------------------------------------------+
```

### Task form wireframe

```
+-----------------------------------------------------------------+
| Task: 8f3a...                                   [Success]       |
+-----------------------------------------------------------------+
|  Server           [ server-blr1-01                ]             |
|  Virtual Machine  [ d4f7c1a2-...-9b3e             ]             |
|  Script           [ provision-vm.sh               ]             |
|  Triggered By     [ aditya@adityahase.com         ]             |
|                                                                 |
|  Variables (JSON)                                               |
|  ┌───────────────────────────────────────────────────────────┐  |
|  │ {                                                         │  |
|  │   "VIRTUAL_MACHINE_NAME": "d4f7c1a2-...",                 │  |
|  │   "IMAGE_NAME": "ubuntu-24.04",                           │  |
|  │   ...                                                     │  |
|  │ }                                                         │  |
|  └───────────────────────────────────────────────────────────┘  |
|                                                                 |
|  Status          [ Success ]                                    |
|  Exit Code       [ 0       ]                                    |
|  Started         [ 2026-05-25 13:11:02.114                  ]   |
|  Ended           [ 2026-05-25 13:11:05.503                  ]   |
|  Duration        [ 3389 ms                                  ]   |
|                                                                 |
|  Stdout                                                         |
|  ┌───────────────────────────────────────────────────────────┐  |
|  │ Provisioned d4f7c1a2-7e0a-4f1b-93cc-ad96b9b39b3e.         │  |
|  └───────────────────────────────────────────────────────────┘  |
|  Stderr                                                         |
|  ┌───────────────────────────────────────────────────────────┐  |
|  │ + install -d -m 0700 /var/lib/atlas/virtual-machines/...  │  |
|  │ + cp ...                                                  │  |
|  └───────────────────────────────────────────────────────────┘  |
+-----------------------------------------------------------------+
```
