# Architecture

## The picture

```
                +----------------------------------+
                |    Atlas (Frappe site)           |
                |    atlas.local                   |
                |                                  |
                |  DocTypes:                       |
                |   - Atlas Settings (Single)      |
                |   - Provider                     |
                |   - DigitalOcean Settings        |
                |   - Scaleway Settings            |
                |   - Self-Managed Settings        |
                |   - Provider Size                |
                |   - Provider Image               |
                |   - Server                       |
                |   - Virtual Machine              |
                |   - Virtual Machine Image        |
                |   - Task                         |
                |                                  |
                |  Desk = the UI                   |
                +-----------------+----------------+
                                  |
                       SSH (root, key-based)
                                  |
              +-------------------+-------------------+
              |                                       |
   +----------v----------+                +-----------v---------+
   |   Server A          |                |   Server B          |
   |   (DO droplet)      |                |   (self-managed)    |
   |                     |                |                     |
   |  /var/lib/atlas/    |                |  /var/lib/atlas/    |
   |    images/          |                |    images/          |
   |    virtual-machines/|                |    virtual-machines/|
   |    bin/             |                |    bin/             |
   |    run/             |                |    run/             |
   |                     |                |                     |
   |  systemd:           |                |  systemd:           |
   |   firecracker-vm@*  |                |   firecracker-vm@*  |
   +---------------------+                +---------------------+
```

## Components

### Atlas (this app)

A Frappe app installed on `atlas.local`. It owns the database of servers and
virtual machines, exposes Desk forms for the operator, and runs background
jobs that SSH into servers to make changes.

There is no Atlas agent on the server. Everything Atlas does on a server is
the result of one SSH invocation that runs one shell script. The script comes
from this repository, under [`atlas/scripts/`](../scripts/). The Frappe code
uploads the script, runs it, and records the result.

### Desk navigation

When the operator logs in, the desk has to surface Atlas. Frappe v16 has
several disjoint discoverability surfaces and an app needs all of them
wired or only some routes work:

- **`add_to_apps_screen` hook** in [`hooks.py`](../atlas/hooks.py) — the
  registration that makes Frappe treat Atlas as a first-class app.
  `frappe.apps.get_default_path()` reads this to decide where to send the
  user on login; without it the user lands on the generic `/app` and has
  to find Atlas themselves. The hook also drives the tile on the `/apps`
  app launcher screen.
- **`Workspace` document `Atlas`** in
  [`atlas/atlas/workspace/atlas/atlas.json`](../atlas/atlas/workspace/atlas/atlas.json)
  — the page rendered at `/app/atlas`. Carries the shortcuts, number
  cards, and DocType card groups. This is what the operator actually
  reads when they arrive.
- **`Workspace Sidebar` document `Atlas`** in
  [`atlas/workspace_sidebar/atlas.json`](../atlas/workspace_sidebar/atlas.json)
  — the left-rail sidebar group with direct links to the five Atlas
  DocTypes. The `Home` item points back at the `Atlas` Workspace.
- **`Desktop Icon`** rows — the launcher tile on `/apps`. Frappe
  generates these from `add_to_apps_screen` via `after_app_install`, so
  on fresh installs nothing in this app needs to do it. The
  [`install_atlas_sidebar`](../atlas/patches/v1_0/install_atlas_sidebar.py)
  patch backfills it for sites where Atlas was installed before the
  hook was added.

The connections between DocTypes (Server → Virtual Machine → Task) are
rendered by Frappe's standard Connections dashboard on each form, driven
by `<doctype>_dashboard.py` next to each DocType controller.

Across all five Atlas doctypes the desk form goes through a small
shared client layer
([`atlas/public/js/atlas_form_overrides.js`](../atlas/public/js/atlas_form_overrides.js),
wired via `doctype_js`) that hides the right rail and timeline, gives
buttons a primary / secondary / danger hierarchy, and demands typed
confirmation for destructive or billable actions. See
[10-desk-ui.md](./10-desk-ui.md) for the full convention.

### Provider abstraction

Atlas talks to vendors through one Python interface (a `Provider` ABC at
`atlas/atlas/providers/base.py`) with five methods: `authenticate`,
`discover`, `provision`, `describe`, `destroy`. One subclass per
vendor; the registry keys off `Provider.provider_type`. Controllers
never branch on the vendor — they call `atlas.get_provider()` and use
the returned object.

Three provider types are implemented:

- **DigitalOcean.** `DigitalOcean Settings` (Single) holds the API
  token, the operating region (Atlas is single-region per vendor), and
  the default size + image Link references. `DigitalOceanProvider`
  wraps the DO REST client at `atlas/atlas/digitalocean.py`.
- **Scaleway.** `Scaleway Settings` (Single) holds the IAM secret key,
  the project id, the operating zone, the billing mode (hourly/monthly),
  and the default size + image. `ScalewayProvider` wraps the Elastic
  Metal (bare-metal) REST client at `atlas/atlas/scaleway.py`. Bare metal
  means a real NVMe-backed LVM pool (no loopback file) and a routed `/64`
  per host (no `/124` carve, so the 15-VM ceiling is gone); the Elastic
  Metal create is async, so `provision()` returns a `delivering` server
  and the worker polls `describe()` until it is `ready` and the OS install
  `completed`. Inbound IPv4 uses a routed **Flexible IP** (the `Reserved
  IP` primitive) instead of a DO anchor. See
  [06-networking.md](./06-networking.md).
- **Self-Managed.** The operator has already built the host (any cloud,
  bare metal, a server in a cupboard). There is no API to call;
  `provision()` validates the operator-supplied IPv4 / IPv6 inputs and
  returns them as the Server's networking. `destroy()` is a no-op.
  `Self-Managed Settings` is an empty stub today.

Cross-vendor configuration lives on `Atlas Settings` (Single): the
active `Provider` link, and the SSH key (fingerprint, public key body,
on-disk path). Vendor catalogs (machine sizes, OS images) live in the
`Provider Size` / `Provider Image` DocTypes — seeded at first run,
refreshed via the Provider form's **Refresh Catalog** button which
calls `provider.discover()`. See [02-doctypes.md](./02-doctypes.md) for
the full schema and [llm/plan/provider-abstraction.md](../llm/plan/provider-abstraction.md)
for the implementation plan.

Both vendor types end up at the same place — a `Server` row Atlas can
SSH into — and from there every other DocType behaves identically. The
abstraction is not designed for multi-cloud orchestration; it keeps the
"how did this host come to exist?" question out of the rest of the
system.

### Server

A `Server` document represents one host. It is created by clicking
"Provision Server" on a `Provider`. For `DigitalOcean` providers this
calls the DO API, then a worker polls `provider.describe()` until the
droplet is ready and writes the IPs / size / image / `provider_metadata`
back to the row. For `Self-Managed` providers the operator types in
the IP and IPv6 details of a host they have already built; `provision()`
returns them as the Server's networking immediately. Both paths end
the same way: wait for SSH, then bootstrap the host.

### Virtual Machine

A `Virtual Machine` document represents one Firecracker microVM running on
one server. The operator picks the server at creation time. The buttons on
the form (`Provision`, `Start`, `Stop`, `Delete`) translate to one task each.

### Virtual Machine Image

A kernel + rootfs pair. Images for this iteration: Ubuntu 24.04 (noble)
server and minimal cloud images from `cloud-images.ubuntu.com`. Image
bytes live on each server under `/var/lib/atlas/images/`; the document
tracks the canonical URLs and checksums.

### Task

Every shell script Atlas runs against a server is persisted as a `Task`
document. It captures the script path, the input variables, stdout, stderr,
exit code, timing, and the user who triggered it. This is our audit log and
our debugger.

## Data flow: provisioning a virtual machine

```
operator clicks Provision on a Virtual Machine
      |
      v
Virtual Machine.status = Pending; enqueue background job
      |
      v
job: allocate IPv6, MAC, tap name in the Frappe DB
      |
      v
scp scripts/provision-vm.py to the server (the atlas package is already durable
at /var/lib/atlas/bin from bootstrap — not re-staged; both calls share one SSH
master via ControlMaster, so only the first pays a handshake)
      |
      v
ssh root@server "PYTHONPATH=/var/lib/atlas/bin python3 /tmp/atlas/provision-vm.py --virtual-machine-name … --flag value …"
      |  -- the script does, on the server, in one process:
      |     - lvcreate -s: instant CoW snapshot of the base image LV -> VM disk LV
      |     - lvextend -r if disk_gigabytes > base (grow LV + ext4)
      |     - mount the LV device, write SSH key + env, umount
      |     - mknod the LV's block node into the jail (per-VM uid, 0660)
      |     - write firecracker.json, network.env, jailer-launch.sh
      |     - systemctl enable + systemctl start --no-block firecracker-vm@<name>.service
      |  (--no-block: the Task returns once systemd queues the start, not once the
      |   unit is active; systemd's ExecStartPre runs vm-disk-up + vm-network-up,
      |   then ExecStart runs the launcher — all async to the Task)
      |
      v
Task is created with status = Running, then updated to Success
      |
      v
Virtual Machine.status = Running
```

One task per lifecycle operation. Not one task per shell command. See
[04-tasks.md](./04-tasks.md).

## What lives where

| State                          | Where                                  | Authoritative? |
| ------------------------------ | -------------------------------------- | -------------- |
| Server IPs, size, image, provider | Frappe DB                           | Yes            |
| Vendor catalogs (sizes, images) | Frappe DB (`Provider Size`, `Provider Image`) | Yes (mirrors vendor; refreshed via `discover()`) |
| Provider credentials           | Frappe DB (per-vendor Settings Single) | Yes            |
| SSH key (fingerprint, public key) | Frappe DB (`Atlas Settings`)        | Yes            |
| SSH private key                | Atlas host disk, `/etc/atlas/keys/*.pem` | Yes (DB stores the path only) |
| VM specs (vCPU, RAM, disk)     | Frappe DB                              | Yes            |
| VM-to-server placement         | Frappe DB                              | Yes            |
| IPv6 address assignments       | Frappe DB                              | Yes            |
| Task history                   | Frappe DB                              | Yes            |
| Image bytes                    | Each server, `/var/lib/atlas/images/`  | No (cache)     |
| `firecracker.json`, rootfs     | Each server, `/var/lib/atlas/virtual-machines/` | No (cache) |
| systemd units                  | Each server, `/etc/systemd/system/`    | No (cache)     |
| Running Firecracker processes  | Each server                            | No (cache)     |

"No (cache)" means: if we lose it, we can rebuild it from the Frappe DB.
We do not parse it back to update Frappe.

## Why no library imports

Why not paramiko, fabric, pyinfra, python-digitalocean? Because:

- This app sits below everything else. Its dependency choices become other
  apps' dependency choices.
- Our SSH usage is "run one script, capture output." That's a `subprocess.run(["ssh", ...])`.
- DigitalOcean's API is HTTPS+JSON. Frappe already has `requests`. A few
  endpoints is fewer than a library.
- pyinfra and zx are inspirations: declarative ops that desugar to shell.
  We want the *idea* (one script per task, idempotent, structured output) —
  not a build-time dependency.

When something gets too painful, we revisit. Not before.
