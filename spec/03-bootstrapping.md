# Bootstrapping a server

A server starts as a vanilla Ubuntu host. Ubuntu 24.04 is the supported
target (this is what the DigitalOcean image gives us). Ubuntu 26.04 is
known to work on Self-Managed hosts but is not part of the regression
suite; if the bootstrap script breaks on 26.04, it is a bug we will fix.
Bootstrap is the task that turns whatever Ubuntu the operator gave us
into a Firecracker host.

## The script

There is one script:
[`atlas/scripts/bootstrap-server.sh`](../scripts/bootstrap-server.sh). It does
everything in a single SSH session. It is the canonical artifact — the spec
is a reading guide, not the source of truth. If the script and this document
disagree, the script wins. Update both.

### Inputs (environment variables)

| Variable               | Notes                                                  |
| ---------------------- | ------------------------------------------------------ |
| `FIRECRACKER_VERSION`  | Pinned in the `Server Provider` defaults, currently `v1.15.1`. |
| `ARCHITECTURE`         | `x86_64` for this iteration.                           |

### What the script does

Read the file. It is ~70 lines.

In summary, in this order:

1. Verifies architecture matches and `/dev/kvm` is readable+writable.
2. Installs `ca-certificates`, `curl`, `e2fsprogs`, `iproute2`, `jq`,
   `nftables`, `squashfs-tools`.
3. Installs Firecracker at `/usr/local/bin/firecracker` if not at the pinned
   version.
4. Writes `/etc/sysctl.d/60-atlas.conf` with IPv6 forwarding and proxy NDP.
5. Creates the `inet atlas` nftables table and `forward` chain.
6. Creates the `/var/lib/atlas/` directory tree.
7. Writes `FIRECRACKER_VERSION`, `KERNEL_VERSION`, `ARCHITECTURE` to
   `/var/lib/atlas/bootstrap.json` (the single source of truth) and
   `cat`s it on stdout.

The Python side `json.loads` the trailing JSON object and writes the
fields onto the `Server` document. `jq` is invoked with `-nc` (compact,
single-line) so the trailing line is a single object; the parser scans
backwards for the last non-empty line.

### Files that must already be on the server

The bootstrap script does not itself fetch helper scripts or the systemd unit
template — uploading them is the caller's job, so that we keep the contents
of `atlas/scripts/` as the single source of truth. Before running
`bootstrap-server.sh`, the caller uploads:

- `scripts/vm-network-up.sh` → `/var/lib/atlas/bin/vm-network-up.sh`
- `scripts/vm-network-down.sh` → `/var/lib/atlas/bin/vm-network-down.sh`
- `scripts/systemd/firecracker-vm@.service` → `/etc/systemd/system/firecracker-vm@.service`

The `Server.bootstrap()` Python method orchestrates this:

```
1. open ssh connection (via `connection_for_server`)
2. upload_files: vm-network-up.sh, vm-network-down.sh, firecracker-vm@.service
   (mkdir of parent directories happens inside upload_files)
3. run_task(server=..., script="bootstrap-server.sh",
            variables={"FIRECRACKER_VERSION": ..., "ARCHITECTURE": ...})
   — scp of bootstrap-server.sh + ssh exec happen inside run_task.
4. parse trailing JSON object from stdout into Server fields
   (firecracker_version, kernel_version, architecture)
5. save the Server row.
```

This is one Task: `bootstrap-server.sh`. The pre-copy step is not a Task,
it's plumbing, and its commands are not interesting individually. They do
appear on stderr of the task because we run the SSH wrapper with `-x`.

## Provisioning a server end-to-end

`Server Provider.provision_server(...)` is whitelisted and called from the
**Provision Server** button. Both provider types funnel into the same
`finish_provisioning` step that actually runs bootstrap.

`finish_provisioning` is enqueued (`frappe.enqueue(..., queue="long")`),
not run inline. The button returns the moment the `Server` row is
inserted — a `bench worker` must be running for the row to leave
`Pending`. With no worker, the Server stays `Pending` forever and there
is no UI signal that anything is wrong. The same applies to
`Virtual Machine Image.sync_to_server` (see [08-images.md](./08-images.md)).

The operator picks a `title` (the user-facing label); the Server row's
`name` is a UUID assigned by `Server.autoname()`. The `provision_server`
controller returns the new UUID — call sites that route to the form
should use the returned name, not the title.

### DigitalOcean

Signature: `provision_server(title, region=None, size=None, image=None)`.
Sync for the cheap part, async for the slow part:

```
1. Validate no existing Server row carries this title.
2. DigitalOceanClient.create_droplet(name=title, ...).
3. Insert a Server row with status = "Pending", a UUID name, the title,
   and provider_resource_id = droplet["id"] (region, size copied from
   the dialog or from provider defaults).
4. frappe.enqueue("...finish_provisioning", queue="long", server_name=<uuid>).
5. Return the new UUID name immediately.
```

The `finish_provisioning(server_name)` worker:

```
1. Load Server, read provider_resource_id from the row.
2. wait_for_active(provider_resource_id, timeout=600s).
3. Write ipv4_address, ipv6_address, ipv6_prefix, and
   ipv6_virtual_machine_range (the /124 carved from the /64) onto the Server.
4. status = "Bootstrapping". Save.
5. wait_for_ssh(connection_for_server(server), timeout=300s).
6. server.bootstrap()  — synchronous inside the worker; no nested enqueue.
7. On success: status = "Active". On any exception: status = "Broken"
   and re-raise so the Task row carries the failure.
```

The worker takes only `server_name` (the row's UUID); the droplet id
lives on the row, so re-running the worker (idempotency check, retry)
does not need the caller to remember it.

### Self-Managed

Signature: `provision_server(title, ipv4_address, ipv6_address,
ipv6_prefix, ipv6_virtual_machine_range)`. There is no droplet to create
and nothing to wait for — the host already exists:

```
1. Validate no existing Server row carries this title.
2. Insert a Server row with status = "Pending", a UUID name, the title,
   provider_resource_id = "" (empty), region / size empty, and the
   IPv4 / IPv6 fields copied from the dialog inputs.
3. frappe.enqueue("...finish_provisioning", queue="long", server_name=<uuid>).
4. Return the new UUID name immediately.
```

`finish_provisioning` on a Self-Managed server skips the "wait for the
provider API" and "write networking fields" steps (they were already
written at insert) and goes straight to:

```
1. status = "Bootstrapping". Save.
2. wait_for_ssh(connection_for_server(server), timeout=300s).
3. server.bootstrap().
4. On success: status = "Active". On any exception: status = "Broken".
```

The worker branches on `server.provider.provider_type`. The two paths
share `wait_for_ssh` and `server.bootstrap()`; only the wait-for-API and
write-networking-fields steps are DO-specific.

### Common: failure handling

A `Broken` server can be re-bootstrapped by clicking **Bootstrap** on the
form because `bootstrap-server.sh` is idempotent. For DigitalOcean the
droplet is left intact for the operator to delete in DO if they choose.
For Self-Managed the host is the operator's problem; Atlas never touches
it beyond SSH.

### Idempotency

Every action is idempotent:

- `apt-get install -y` is idempotent.
- The Firecracker install is gated on `firecracker --version`.
- File writes use `install -m mode -T` (atomic, overwrite).
- nftables creates are guarded with `nft list ... || nft add ...`.
- `mkdir -p` and `systemctl daemon-reload` are naturally idempotent.

Re-running `Bootstrap` is the recovery path. There is no separate "repair"
mode and there will not be one.

### Pinned versions

`FIRECRACKER_VERSION = v1.15.1`. To bump, edit the default on the
`Server Provider`, re-run `Bootstrap` on every server. The script is
idempotent so re-running is the only thing the operator does.

`ARCHITECTURE = x86_64`. `aarch64` is on the roadmap.

### Failure modes

| Failure                          | Resulting Server status | Operator action               |
| -------------------------------- | ----------------------- | ----------------------------- |
| SSH never comes up               | `Pending`               | Investigate the droplet on DO.|
| `/dev/kvm` missing               | `Broken`                | Wrong droplet size — recreate.|
| `apt-get` fails                  | `Broken`                | Re-run Bootstrap.             |
| Firecracker download fails       | `Broken`                | Re-run Bootstrap.             |
| Architecture mismatch            | `Broken`                | Wrong droplet image — recreate.|

There is no automatic retry. The escape hatch is the same code path: click
`Bootstrap` again. The Task list shows every attempt.

## Why a shell script (and not pyinfra)

Read [04-tasks.md](./04-tasks.md). Short version: pyinfra's idea — declarative
ops desugared to commands per host — is good. The implementation is too much
machinery for a building block. A shell script is a single file, readable
top-to-bottom, and runs in one process on the server. When pain forces a
better abstraction, we will reach for it then, and we will likely build a
small subset of pyinfra ourselves instead of taking the dependency. See the
[roadmap](./09-roadmap.md).
