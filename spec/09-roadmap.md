# Roadmap and deferred decisions

This iteration is a building block. Two flavors of deferral live here:

1. Things we know we'll do later, with a cheap path to add them.
2. Architectural questions we punted on, and how we plan to revisit them.

## Punted decisions

### SSH execution model — "one Task = one shell script"

We picked this because it's the smallest model that lets a VM provision in
one network round-trip and produces a single audit row per operation. It
trades step-level error attribution for speed and simplicity.

The shape we'd eventually want is something like
[`pyinfra`](https://pyinfra.com): declare desired state in Python, get
batched per-host shell commands and structured outputs. We didn't take the
dependency because:

- pyinfra is a substantial framework (operations, facts, connectors,
  gevent). It assumes a deploy-from-CLI workflow we don't have.
- We can grow a 200-line subset ourselves when the pain shows up.

When to revisit: when we have more than ~3 scripts that share large blocks
of "ensure file exists / ensure package installed" logic. At that point,
extract a tiny operations layer in Python, keep each operation idempotent,
keep the Task-per-script contract.

### Bootstrap mechanism — shell script today

Same shape as above. A Bash script is the smallest thing that works. When
servers grow distinct roles (compute, edge, builder) or when we want to
*declare* their state and reconcile it, we will build a small declarative
layer ourselves rather than take pyinfra.

When to revisit: when there are two genuinely different bootstrap paths,
or when an operator wants to make a small surgical change to a running
server without re-running the whole script.

### Address reuse on archive

Today, archived VMs hold their IPv6 address forever; new VMs always get a
fresh address from the /124. With a /124 (15 usable addresses) this caps
the lifetime number of VMs per server at 15.

This is acceptable for the building block but obviously not for production.
The next iteration moves to either:

- Larger usable subnets per server (talk to DO; or move off DO to a
  provider that routes the whole /64 to the droplet), or
- Reusing addresses with a quarantine window (Task audit gets a "this
  address was used by VM X 2026-05-01..2026-05-04" lookup).

### Host-key trust

We use `StrictHostKeyChecking=accept-new`. First connection is
trust-on-first-use. A compromised DigitalOcean control plane could swap a
droplet underneath us between bootstrap and first SSH. Fix is to capture
the host key during `Server.provision()` (right after droplet create, via
the DO API's serial console — or by reading the public key from the
droplet's `/etc/ssh/ssh_host_ed25519_key.pub` over the *first* SSH and
pinning it). Both add a field to `Server` and a one-time write. Not
breaking.

## Near-term hedges

Cheap structural changes to make **near-term** (not now — but before any
production load), because they're much more expensive to retrofit later than
to set up early. These are not on the lists above. None change current
behavior; they just keep doors open.

- **Secret indirection for SSH keys and provider tokens.** Keep the
  fields on `Atlas Settings` / per-vendor Settings, but route reads
  through a single helper so the storage backend can be swapped to an
  external secret store without touching callers. DB-as-keystore is
  fine for the PoC; not fine once customers exist.
- **SSH scripts call `sudo` explicitly.** No-op as `root` today, but turns
  the planned move to an unprivileged user (see below) from "rewrite every
  script" into "create the user."
- **Spill Task `stdout`/`stderr` over N KB to a file.** The Task row keeps a
  capped excerpt + a pointer. Avoids the DocType becoming a log store.

- **Key the image-sync short-circuit to guest content, not just the rootfs.**
  `sync-image.sh` exits early ("rootfs already built") when the unpacked rootfs
  is present, but the guest systemd unit
  ([`scripts/guest/atlas-network.service`](../scripts/guest/atlas-network.service))
  is baked in at sync time — so a change to the guest unit (as the NAT44 egress
  work made) is **invisible** to an already-synced server until the rootfs is
  rebuilt for some other reason. Today the escape hatch is the immutable-image
  contract: any change to a spec image field (e.g. `rootfs_filename`) makes
  [`_image.py::ensure_image_row()`](../atlas/atlas/doctype/virtual_machine_image/virtual_machine_image.py)
  delete-and-reinsert the row, forcing a rebuild. That works but is indirect.
  The fix is to stamp a content digest of the guest payload into the image row
  and key the short-circuit on it. Additive; not now.

## Concrete next steps after this iteration

- **Stuck-task reaper**. A scheduled job that looks at Tasks in `Running`
  state older than 2× their declared timeout and marks them `Failure` with
  a synthetic "worker presumed dead" note. The e2e harness already does
  this via `mark_orphan_tasks_failure`; production needs the same
  guarantee. Pair with the "Server lock doctype" if we ever want
  concurrent-sync protection. Additive.

- **Server lock doctype**. A single-row lock keyed by `(server, resource)`
  that long-running mutating Tasks (sync-image, provision) take before
  doing work. Today two concurrent syncs of the same image-on-server are
  a benign race that wastes bandwidth; with more operators it stops being
  benign. Additive.

- **Jailer** — *done*. Every Firecracker process runs under the `jailer`
  binary: de-privileged to a per-VM uid/gid (derived from the UUID, no
  allocator, no passwd row), chrooted into the VM's own jail
  (`virtual-machines/<uuid>/jail/firecracker/<uuid>/root`), with per-VM
  cgroup-v2 caps (`memory.max` = guest RAM + 256 MiB headroom,
  `memory.swap.max=0`, `cpu.max` = vCPUs' bandwidth) and fd/file rlimits, and
  its own network namespace (veth-bridged to the host, IPv6 + NAT44 v4
  reachability preserved). See [05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md),
  [06-networking.md](./06-networking.md), [07-filesystem-layout.md](./07-filesystem-layout.md).
  Still deferred here:
  - **Unprivileged SSH transport.** Atlas still connects to the host as `root`
    to run Tasks. Moving to an `atlas` user with `sudo` on a narrow allowlist
    touches the wrapper that prepends `sudo` and the SSH connection layer (which
    user the key authenticates as). The jailer already removed the need for the
    *Firecracker* process to run as root; this is the remaining root surface.
    Not breaking.
  - **AppArmor profile.** Firecracker ships an AppArmor profile meant to be used
    *with* the jailer for defense in depth. We run the jailer without it for now;
    adding it pairs naturally with the unprivileged-user move. Additive.
  - **CPU pinning.** We cap CPU *bandwidth* (`cpu.max`), not affinity. Pinning
    (`cpuset.cpus`/`cpuset.mems`, NUMA) needs host-topology modeling we don't do
    yet. Additive.
  - **New PID namespace per VM** (`--new-pid-ns`), **custom seccomp filters**,
    and **block/net rate limiters** — extra isolation/tuning knobs on top of the
    jailer + Firecracker defaults. Additive.
  - **Existing-VM migration.** VMs provisioned before this change keep their old
    non-jailed unit and flat (non-jail) paths until re-provisioned; they are not
    retro-jailed. Terminate + reprovision to adopt the jail.

- **More host hardening, deferred from the host-hardening iteration**:
  `/tmp` and `/dev/shm` mount options (`nodev,nosuid,noexec` — CIS 1.1.2.x,
  awkward on a cloud image where `/tmp` is not a separate mount), `auditd`
  with a tuned rule set (a whole subsystem with real log volume), and
  **surfacing "reboot pending"** after an unattended security-kernel update
  (we deliberately do *not* auto-reboot, because that would kill running VMs —
  so a health check should flag hosts that need an operator-scheduled reboot).
  All additive.

- **Host-key pinning**. See above.

- **CLI**. A small `atlas` CLI that calls Frappe's REST API. The DocType
  methods we expose for buttons become the CLI's commands. Pure additive.

- **Multi-arch**. Drop the `ARCHITECTURE` hard-coding; allow `aarch64`. The
  Ubuntu cloud archive publishes arm64 squashfs + `unpacked/` kernels per
  release. Additive on `Server` and the image record.

- **Ubuntu image discovery**. A "Refresh Ubuntu Images" action that scrapes
  `cloud-images.ubuntu.com` (release dirs + `SHA256SUMS`) and upserts a
  catalog, so operators pick a release × variant instead of hand-copying
  `DEFAULT_IMAGE`/`MINIMAL_IMAGE` constants. Mirrors `provider.discover()` /
  the Provider **Refresh Catalog** button. Today the images are pinned
  constants (server + minimal noble); this is the additive follow-up.

- **Newer guest release**. Bump the supported guest to Ubuntu 26.04 once it's
  validated as a guest (it boots; the normalization checklist in
  [08-images.md](./08-images.md) is the regression gate). Additive — a new
  image row, same code path.

## Things on the longer-term list

- **Custom images** (`Virtual Machine Image Build`): build an ext4 from a
  Dockerfile or debootstrap recipe, push to a bucket, point the image
  record at it. Additive.

- **Overlayfs-backed rootfs**: shrink per-VM disk by ~10×. Internal to
  `provision-vm.sh` and `terminate-vm.sh`. Additive.

- **Snapshots** — *done (disk-only)*. Implemented as a copy of the VM's
  `rootfs.ext4` into a `Virtual Machine Snapshot` DocType, with
  restore/rebuild, clone, resize and pause/resume alongside. See
  [05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md) and
  [02-doctypes.md § Virtual Machine Snapshot](./02-doctypes.md#virtual-machine-snapshot).
  Still deferred here:
  - **Firecracker memory-state snapshots** (`/snapshot/create` + `/snapshot/load`).
    These would let an operator resume a *running* VM (RAM included) and do
    true live clones. They need a forked boot path (load is pre-boot-only,
    incompatible with `--config-file`), a RAM-sized memory file kept for the
    VM's lifetime, and a guest-side identity-rotation story for the
    duplicate-state hazard. Out of scope until there's a concrete need.
  - **Snapshot retention / GC / quotas.** Today snapshots are created and
    deleted by hand, guarded only by a `df` pre-flight in `snapshot-vm.sh`.
    A scheduled reaper and per-server disk quotas belong here before any
    real load.
  - **Cross-server snapshots.** A snapshot lives on its VM's server; clone and
    restore target the same server. Moving a snapshot to another host (for
    rebalancing or as an image-build input) is additive but unbuilt.

    It is **not** blocked by the Firecracker cross-host snapshot matrix
    (identical CPU model / host-kernel / GIC version — see
    [snapshot-support.md § "Where can I resume my snapshots?"](../../references/firecracker/docs/snapshotting/snapshot-support.md)).
    Those constraints bind only the serialized *memory-state* snapshot, which we
    deliberately do not use; a disk snapshot is a plain `rootfs.ext4` file and is
    portable. The real blockers are Atlas-side and mundane:
    - **Structural.** Snapshots are children of one VM's UUID directory and die
      with it (`on_trash`, terminate `rm -rf`); the DocType hard-binds
      `virtual_machine` (`set_only_once`) and a read-only denormalized `server`.
      A transferable snapshot needs a host-independent store and a mutable
      location — a DocType + on-disk-layout change. Largest piece.
    - **Kernel pairing.** A disk snapshot carries no kernel; clone/restore take
      it from `source_image`. The target host must already have the matching
      `Virtual Machine Image` synced (reuse the `provision-vm.sh` step-0
      image-present precondition).
    - **Transfer cost.** We use plain `cp` (no overlayfs/CoW/reflink; ext4 has
      no CoW), so a transfer is a full N-GB stream. The naive slice (`cp`/rsync
      over SSH, full copy, fail-loud) is in-grain; the fast slice (incremental /
      thin-pool send) depends on the **overlayfs-backed rootfs** item above or
      the **LVM thin-pool** backlog idea.
    - **Trust boundary.** Firecracker trusts snapshot files and does only a CRC;
      moving bytes host→host is exactly where it says auth + encryption are
      required. Atlas has no host↔host trust (each host trusts only Atlas) and no
      at-rest rootfs encryption today — both are gaps to close before this is a
      customer-facing transfer, not just operator rebalancing.
    - **Networking.** `ipv6_address` is allocated per-server from
      `Server.ipv6_virtual_machine_range`, so a transferred snapshot can only
      feed a **clone on the target** (fresh identity, new IP) — that path is
      unblocked today. *VM mobility* (same VM, same IP, new host, e.g. draining a
      host for maintenance) additionally requires the **floating-IP** backlog
      idea as a hard predecessor.
    - **Operations.** A multi-minute transfer is a long mutating Task on two
      hosts at once; it wants the **Server lock doctype** and **stuck-task
      reaper** (above) before real load.

    Aside (snapshot security, independent of transfer): the guest's 512 MiB
    `/swapfile` lives *inside* `rootfs.ext4`, so every disk snapshot captures
    guest swap contents — a data-remanence concern when a snapshot is cloned
    across a tenant boundary. The Firecracker prod-host rec to disable swap is
    about *host* swap; this is the in-guest analogue and belongs in the
    snapshot-security discussion when tenancy lands.

- **Health checks**: a scheduled job that runs `systemctl is-active …` per
  VM and reconciles `Virtual Machine.status`. Additive.

- **Metrics**: `firecracker --metrics-path` per VM, shipped to whatever
  metrics store the next layer cares about. Additive.

- **Console access**: signed URL to the serial console via the API socket.
  Needs a small web service. Additive.

- **Quotas / ownership / scheduling**: belongs in the layer above Atlas.
  Atlas gains a `team` field on resources but stays unaware of policy.

## Things we will not do, regardless

- Build our own hypervisor.
- Build a portal. Desk and a future CLI cover what we need.
- Adopt Kubernetes.
- Multi-tenant secrets management in this app.

## Changes

- `v0.1` — initial spec.
- `v0.2` — renamed `Metal Node`→`Server`, `Metal Command`→`Task`,
  `VM Image`→`Virtual Machine Image`. Switched from paramiko to system
  `ssh`. One Task = one shell script. Bumped Firecracker to v1.15.1.
  Documented the DigitalOcean /124 routing constraint. VMs are now UUIDs
  and keep their name on archive. Shell scripts live in `atlas/scripts/`,
  not embedded in markdown.
- `v0.3` — added the `Self-Managed` provider type. `Provision Server`
  now takes IPv4/IPv6 inputs for self-managed hosts instead of calling a
  cloud API. `ipv6_virtual_machine_range` is no longer assumed to be a
  /124 — any prefix length is accepted. Ubuntu 26.04 is acknowledged as
  a working (but untested) host OS.
- `v0.4` — **IPv4 egress via host NAT44.** Each VM gets a private /30 on
  `eth0` (derived from its IPv6 host-index inside `100.64.0.0/16`, no new
  DocType/field/allocator) plus a v4 default route; the host runs
  `net.ipv4.ip_forward=1` and one host-wide masquerade rule in the
  `inet atlas` `postrouting` chain. Egress-only — no inbound v4, no per-VM
  public v4; IPv6 stays the identity and the only inbound path. Verified
  end-to-end: a booted guest reaches an IPv4-only literal through the
  masquerade. See [06-networking.md § IPv4 egress (NAT44)](./06-networking.md).
- `v0.5` — host hardening at bootstrap (CIS 3.3 sysctls, an sshd_config.d
  drop-in, a kernel-module blocklist, unattended security updates, KSM/swap
  off), expressed as portable `*.d` drop-ins. Three deliberate CIS deviations
  documented (forwarding stays on — for both v4 and v6 — `squashfs` kept, and
  `PermitRootLogin prohibit-password`). Atlas still operates as root; the
  unprivileged-user + jailer + AppArmor privilege-drop remains deferred.
