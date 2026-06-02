# Bootstrapping a server

A server starts as a vanilla Ubuntu host. Ubuntu 24.04 is the supported
target (this is what the DigitalOcean image gives us). Ubuntu 26.04 is
known to work on Self-Managed hosts but is not part of the regression
suite; if the bootstrap script breaks on 26.04, it is a bug we will fix.
Bootstrap is the task that turns whatever Ubuntu the operator gave us
into a Firecracker host.

## The script

There is one script:
[`atlas/scripts/bootstrap-server.py`](../scripts/bootstrap-server.py). It does
everything in a single SSH session. It is the canonical artifact — the spec
is a reading guide, not the source of truth. If the script and this document
disagree, the script wins. Update both. Like every task it is a typed Python
program (see [04-tasks.md § Tasks are Python](./04-tasks.md)): its inputs are
`--kebab-case` CLI flags and it emits one `ATLAS_RESULT=` JSON line carrying the
host facts the controller records.

### Inputs (CLI flags)

The controller's `variables` dict (UPPER_SNAKE keys) is rendered to
`--kebab-case` flags by the runner, parsed by `BootstrapInputs.from_args()`.

| Variable / flag                            | Notes                              |
| ------------------------------------------ | ---------------------------------- |
| `FIRECRACKER_VERSION` → `--firecracker-version` | Pinned in `atlas/atlas/doctype/server/server.py`, currently `v1.15.1`. |
| `ARCHITECTURE` → `--architecture`          | `x86_64` for this iteration.       |

### What the script does

Read the file. It is ~250 lines.

In summary, in this order:

1. Verifies architecture matches and `/dev/kvm` is readable+writable.
2. Waits for the apt locks to clear, then installs `ca-certificates`,
   `curl`, `e2fsprogs`, `iproute2`, `jq`, `lvm2`, `nftables`,
   `squashfs-tools`, `thin-provisioning-tools`.
   A freshly-booted cloud image still has cloud-init / unattended-upgrades
   running its own `apt-get` for the first minutes, holding the apt locks;
   the script blocks on `cloud-init status --wait` and then polls the
   apt/dpkg lock files (capped) before touching apt. Without this, the very
   first `apt-get update` raced cloud-init and failed with
   "Could not get lock /var/lib/apt/lists/lock", landing fresh droplets in
   `Broken`. (apt's `DPkg::Lock::Timeout` is set too, but it does not cover
   the `apt-get update` *lists* lock on this apt version, so the explicit
   wait is the load-bearing fix.)
3. Installs Firecracker **and the jailer** at `/usr/local/bin/{firecracker,jailer}`
   if either is missing or not at the pinned version. Both ship in the same
   release tarball, so this is one download. Production runs every VM under the
   jailer; a host bootstrapped before the jailer existed picks it up on re-run
   (the gate checks both binaries).
4. Writes `/etc/sysctl.d/60-atlas.conf` with IPv6 forwarding, proxy NDP, and
   IPv4 forwarding (`net.ipv4.ip_forward`, for NAT44 egress), **plus the
   CIS 3.3 network-hardening sysctls** — see *Host hardening* below.
5. Writes the sshd hardening drop-in, the kernel-module blocklist, enables
   unattended security updates, and disables KSM/swap — see *Host hardening*.
6. Creates the `inet atlas` nftables table with a `forward` chain (IPv6
   filter) and a `postrouting` chain holding the host-wide IPv4 masquerade
   rule. See [06-networking.md](./06-networking.md).
7. Creates the `/var/lib/atlas/` directory tree.
8. Creates the **LVM thin pool** that backs every VM disk: loads `dm_thin_pool`
   (persisted via `/etc/modules-load.d/60-atlas-lvm.conf`), then runs the
   idempotent `atlas_pool_ensure` — a sparse backing file at
   `/var/lib/atlas/pool/atlas-pool.img`, a loop device over it, a PV/VG
   (`atlas`), and a thin pool LV (`pool0`, with an explicit
   `--poolmetadatasize 1G`). The PV is a **loopback file** because a stock
   droplet has no spare block device; the only line that changes for a real
   attached device (the spec/09 follow-on) is the loop binding. Bootstrap is
   **not** re-run on reboot, so it also enables `atlas-pool.service` — a oneshot
   that imports the durable package (`from atlas.lvm import ThinPool`) and calls
   `ThinPool().ensure()` to re-assert the pool's loop device on boot, ordered
   before the VM units. See [07-filesystem-layout.md](./07-filesystem-layout.md).
9. Writes `FIRECRACKER_VERSION`, `JAILER_VERSION`, `KERNEL_VERSION`,
   `ARCHITECTURE` to `/var/lib/atlas/bootstrap.json` (the single source of
   truth) and `cat`s it on stdout. `firecracker_version` and `jailer_version`
   are always the same (one tarball) but both are recorded on the `Server` row.

The Python side `json.loads` the trailing JSON object and writes the
fields onto the `Server` document. `jq` is invoked with `-nc` (compact,
single-line) so the trailing line is a single object; the parser scans
backwards for the last non-empty line.

### Host hardening

Bootstrap hardens the host as part of the same idempotent script. The
controls are a cherry-picked subset of the **Firecracker production-host
setup** doc and the **CIS Ubuntu 24.04 / CIS Distribution-Independent
Linux** benchmarks — chosen because they reduce real attack surface on a
microVM host without breaking it, and skipping everything that is box-ticking
for a headless, key-only-root, machine-controlled host (no PAM/password
policy, no AIDE, no auditd, no login banners, no service-disable sweep). All
controls are expressed as `*.d` drop-in files (sysctl.d, sshd_config.d,
modprobe.d, apt.conf.d) so they are idempotent overwrites and portable across
Ubuntu 24.04 and 26.04 — we never invoke a release-pinned hardening tool.

The hardening is **not** a separate operation, button, or Task: it is part of
`bootstrap-server.py`, re-applied (as a no-op) on every re-bootstrap.

| Control | What | Benchmark |
| --- | --- | --- |
| Network sysctls | reject ICMP redirects, no source routing, no redirect-send, log martians, bogus/broadcast ICMP ignored, SYN cookies, IPv6 `accept_ra=0` — all in `/etc/sysctl.d/60-atlas.conf` alongside the forwarding lines | CIS 3.3.2–3.3.11 |
| sshd drop-in | `/etc/ssh/sshd_config.d/60-atlas.conf`: key-only root, no password/empty-password/keyboard-interactive auth, `MaxAuthTries 4`, `LoginGraceTime 60`, `ClientAlive 300×3`, modern Ciphers/MACs/KexAlgorithms. Validated with `sshd -t` **before** reload so a bad drop-in can never brick SSH | CIS 5.1 |
| Module blocklist | `/etc/modprobe.d/60-atlas-blocklist.conf`: unused filesystem modules (`cramfs`, `freevxfs`, `hfs`, `hfsplus`, `jffs2`, `udf`, `usb-storage`) and unused network protocols (`dccp`, `tipc`, `rds`, `sctp`). It must **never** list a load-bearing module — `tun`/`tap` (VM taps), `kvm`/`kvm_intel`/`kvm_amd` (Firecracker), `vhost`/`vhost_net` (virtio), `nf_tables`/`nft_*` (firewall), `dm_mod`/`dm_thin_pool` (the thin-pool VM-disk backend); CIS only blocklists *unused* modules, so none of these appear, but the e2e probe asserts it. | CIS 1.1.1, 3.2 |
| Security updates | install `unattended-upgrades`, scoped to the **security** pocket only, **no** automatic reboot (a reboot would kill running VMs) | CIS 1.2.2.1 |
| KSM / swap off | disable Kernel Samepage Merging (cross-VM memory side channel) and swap (guest RAM remanence on disk) | Firecracker prod-host |

#### Deliberate deviations

Three benchmark items are **intentionally not applied** because they would
break Atlas. A CIS audit will flag these three as failures — they are
deliberate, documented here, and asserted by the e2e probe so they cannot
silently regress:

1. **IP forwarding stays on** (CIS 3.3.1 says disable it). The VM networking
   model is a routed-tap topology — there is no bridge; the host routes packets
   between its uplink and each per-VM tap, which *is* IP forwarding. With it
   off, every VM is unreachable in both directions. Blast radius is contained at
   the `inet atlas` nftables forward chain, not at the global switch. See
   [06-networking.md](./06-networking.md).
2. **`squashfs` is not blocklisted** (CIS 1.1.1.7 says blocklist it). `unsquashfs`
   unpacks the rootfs image at sync time; blocklisting the module would break
   image sync. The rest of the CIS module blocklist is applied.
3. **`PermitRootLogin prohibit-password`** (CIS 5.1.20 says `no`). Atlas connects
   as root over SSH with a key; there is no unprivileged user yet (that is a
   [roadmap](./09-roadmap.md) item). `no` would lock Atlas out of every server.
   `prohibit-password` is the CIS-acceptable middle form: key-only root, no
   password login.

#### Not done here (still deferred)

Hardening this iteration is **host-level, as root**. The privilege-drop —
an unprivileged `atlas` user, the Firecracker **jailer**, and the Firecracker
**AppArmor** profile — is a larger, breaking change and remains on the
[roadmap](./09-roadmap.md), along with `/tmp` `/dev/shm` mount hardening,
`auditd`, and surfacing "reboot pending" after an unattended security update.

#### What we deliberately skip (and won't re-litigate)

The selection axis is *does this protect a Firecracker host without breaking it,
in a way we can explain in one line and maintain* — not "what a CIS scan scores".
So we **do not** run the full `usg`/CIS profile (it sets the three deviations
wrong and drags in a long tail of PAM/password-policy, AIDE, auditd, and banner
controls that are pure box-ticking on a headless, key-only-root, machine-driven
host); `usg` is at most an audit *reporter*, never the apply mechanism. We also
skip the Firecracker doc's hardware/boot-cmdline items — `nosmt` (halves a
2-vCPU droplet; a multi-tenant-with-hostile-neighbors concern), ECC/TRR memory
and early microcode (provider procurement), and cgroup/`quiet loglevel` GRUB
tuning (don't fit an idempotent re-runnable bootstrap). These are provider- or
tenancy-level concerns that sit above Atlas; revisit only with a concrete need.

### Files that must already be on the server

The bootstrap script does not itself fetch the systemd-invoked hooks, the
systemd units, or the shared package — uploading them is the caller's job, so
that we keep the contents of `atlas/scripts/` as the single source of truth.
These are **durable** state (they live under `/var/lib/atlas/bin` and
`/etc/systemd/system`, not the per-Task `/tmp/atlas` staging), so
`Server.bootstrap()` places them directly via `upload_files`, not through the
per-Task sidecar mechanism. Before running `bootstrap-server.py`, the caller
uploads (see `_BOOTSTRAP_UPLOADS` + `_bootstrap_uploads()` in `server.py`):

- `scripts/vm-network-up.py` → `/var/lib/atlas/bin/vm-network-up.py`
- `scripts/vm-network-down.py` → `/var/lib/atlas/bin/vm-network-down.py`
- `scripts/vm-disk-up.py` → `/var/lib/atlas/bin/vm-disk-up.py`
- `scripts/systemd/firecracker-vm@.service` → `/etc/systemd/system/firecracker-vm@.service`
- `scripts/systemd/atlas-pool.service` → `/etc/systemd/system/atlas-pool.service`
- every `scripts/lib/atlas/*.py` (test files skipped) → `/var/lib/atlas/bin/atlas/*.py`

The systemd hooks (`vm-network-up.py`, `vm-network-down.py`, `vm-disk-up.py`)
are invoked by the unit as `python3 <path> %i` (a positional VM uuid, not Task
`--flags`) and `import` the durable package next to them; the package
(`/var/lib/atlas/bin/atlas/`) replaces the old durable `lvm.sh` shell library.

The `Server.bootstrap()` Python method orchestrates this:

```
1. open ssh connection (via `connection_for_server`)
2. upload_files: the durable hooks, both systemd units, and the atlas package
   (mkdir of parent directories happens inside upload_files)
3. run_task(server=..., script="bootstrap-server.py",
            variables={"FIRECRACKER_VERSION": ..., "ARCHITECTURE": ...})
   — scp of bootstrap-server.py (+ its staged atlas package under /tmp/atlas)
   and the ssh exec happen inside run_task.
4. parse the ATLAS_RESULT= line from stdout into Server fields
   (firecracker_version, jailer_version, kernel_version, architecture);
   the same JSON is also persisted on the host at /var/lib/atlas/bootstrap.json.
5. save the Server row.
```

This is one Task: `bootstrap-server.py`. The pre-copy step is not a Task,
it's plumbing, and its commands are not interesting individually. They do
appear on stderr of the task because Python tasks echo each command (the
`set -x` equivalent the `atlas._run.run` wrapper prints).

## Provisioning a server end-to-end

`Provider.provision_server(...)` is whitelisted and called from the
**Provision Server** button. It calls the provider implementation
(`atlas.get_provider().provision(request)`) in the web request, then
enqueues `finish_provisioning` to run `describe()` (DigitalOcean) or
no-op (Self-Managed) and run bootstrap.

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

### The Provider interface boundary

The controller does not know which vendor it is talking to. It builds a
`ProvisionRequest` dataclass from the dialog inputs and hands it to
`atlas.get_provider().provision(request)`. The result is a
`ProvisionResult` carrying `provider_resource_id`, `ready`, and
optionally a `ServerNetworking` block. Two contracts the interface
enforces:

- `provision()` must return within ~30 seconds. Long-running vendor
  creates (Scaleway Elastic Metal, AWS spot) return `ready=False` with
  a placeholder id; the worker polls `describe()` until ready.
- `describe()` is the authoritative source for Server fields after
  provision. The worker writes `size`, `image`, IPs,
  `ipv6_virtual_machine_range`, and `provider_metadata` from its result
  — `provision()`'s output is treated as a hint, not the truth.

See [02-doctypes.md § Provider abstraction](./02-doctypes.md#provider) and
[llm/plan/provider-abstraction.md](../llm/plan/provider-abstraction.md)
for the full interface.

### DigitalOcean

Signature: `provision_server(title, size=None, image=None)`. The region
is fixed at `DigitalOcean Settings.region` (Atlas is single-region);
the dialog has no region field, and the controller throws if a request
carries one. Sync for the cheap part, async for the slow part:

```
1. Validate no existing Server row carries this title.
2. atlas.get_provider().provision(ProvisionRequest(
       title, size, image, ssh_key=atlas.get_ssh_key(), networking=DUAL_STACK
   )) → ProvisionResult(provider_resource_id=droplet_id, ready=False, ...)
3. Insert a Server row with status = "Pending", a UUID name, the title,
   provider_resource_id from the result. size / image left empty —
   describe() will fill them on the worker side.
4. frappe.enqueue("...finish_provisioning", queue="long", server_name=<uuid>).
5. Return the new UUID name immediately.
```

The `finish_provisioning(server_name)` worker:

```
1. Load Server.
2. identifier = Server.provider_resource_id or Server.name
   — Self-Managed has no vendor-side id; the worker passes the row's
     UUID so describe() can look it up.
3. result = wait_until_ready(provider, identifier, timeout=600s)
   — polls provider.describe() at 5s intervals until ready=True.
4. Apply result.networking to Server: ipv4_address, ipv6_address,
   ipv6_prefix, ipv6_virtual_machine_range (DO: /124 carved from /64).
5. Apply result.size, result.image, result.provider_metadata.
   Empty size / image are skipped (Self-Managed returns "") so
   operator-entered values are not clobbered.
6. status = "Bootstrapping". Save.
7. wait_for_ssh(connection_for_server(server), timeout=300s).
8. server.bootstrap()  — synchronous inside the worker; no nested enqueue.
9. On success: status = "Active". On any exception: status = "Broken"
   and re-raise so the Task row carries the failure.
```

The worker takes only `server_name` (the row's UUID); the droplet id
lives on the row, so re-running the worker (idempotency check, retry)
does not need the caller to remember it.

### Self-Managed

Signature: `provision_server(title, ipv4_address, ipv6_address,
ipv6_prefix, ipv6_virtual_machine_range)`. There is no droplet to create
and nothing to wait for — the host already exists. The controller
builds a `ProvisionRequest` with `prebuilt_networking` populated and
calls `provision()`:

```
1. Validate no existing Server row carries this title.
2. atlas.get_provider().provision(ProvisionRequest(
       title, prebuilt_networking=ServerNetworking(ipv4, ipv6, prefix, range), ...
   )) → ProvisionResult(provider_resource_id="", ready=True, networking=...)
3. Insert a Server row with status = "Pending", a UUID name, the title,
   the operator-supplied IPv4 / IPv6 fields, empty provider_resource_id,
   empty size / image.
4. frappe.enqueue("...finish_provisioning", queue="long", server_name=<uuid>).
5. Return the new UUID name immediately.
```

`finish_provisioning` on a Self-Managed server: the
`SelfManagedProvider.describe()` returns the row's existing networking
unchanged with `ready=True`, so the polling loop exits on the first
iteration with no field updates. Then:

```
1. status = "Bootstrapping". Save.
2. wait_for_ssh(connection_for_server(server), timeout=300s).
3. server.bootstrap().
4. On success: status = "Active". On any exception: status = "Broken".
```

The worker does not branch on provider type — both paths run the same
`wait_until_ready → apply networking → bootstrap` sequence. The
vendor-specific behavior lives entirely inside `provider.describe()`.

### Common: failure handling

A `Broken` server can be re-bootstrapped by clicking **Bootstrap** on the
form because `bootstrap-server.py` is idempotent. For DigitalOcean the
droplet is left intact for the operator to delete in DO if they choose.
For Self-Managed the host is the operator's problem; Atlas never touches
it beyond SSH.

### Idempotency

Every action is idempotent:

- `apt-get install -y` is idempotent (and waits out the first-boot apt-lock
  race before running — see step 2).
- The Firecracker + jailer install is gated on `firecracker --version` and
  `jailer --version` (re-run installs either if absent or wrong-versioned).
- File writes use `install -m mode -T` (atomic, overwrite). The hardening
  drop-ins (sysctl.d, sshd_config.d, modprobe.d, apt.conf.d) are all written
  this way, so a re-bootstrap rewrites identical bytes — a clean no-op.
- nftables creates are guarded with `nft list ... || nft add ...`.
- `sshd -t` validates the drop-in before `systemctl reload ssh`; `swapoff -a`
  and the KSM write are no-ops when already off.
- `mkdir -p` and `systemctl daemon-reload` are naturally idempotent.

Re-running `Bootstrap` is the recovery path. There is no separate "repair"
mode and there will not be one.

### Pinned versions

`FIRECRACKER_VERSION = v1.15.1`. To bump, edit the constant in
`atlas/atlas/doctype/server/server.py` and re-run `Bootstrap` on every
server. The script is idempotent so re-running is the only thing the
operator does.

`ARCHITECTURE = x86_64`. `aarch64` is on the roadmap.

### Failure modes

| Failure                          | Resulting Server status | Operator action               |
| -------------------------------- | ----------------------- | ----------------------------- |
| SSH never comes up               | `Pending`               | Investigate the droplet on DO.|
| `/dev/kvm` missing               | `Broken`                | Wrong droplet size — recreate.|
| `apt-get` fails                  | `Broken`                | Re-run Bootstrap. (First-boot apt-lock race is waited out in step 2.) |
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
