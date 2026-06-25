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

A related, narrower guard is deferred for **subdomain routing**
([18-bench-self-routing.md](./18-bench-self-routing.md) Component F): a `/128`
should not be re-handed by `allocate_ipv6` while a `Subdomain` still references it
(else a stale route briefly points at a recycled address — a cross-tenant leak).
The v1 mitigation is `VirtualMachine.terminate()` deleting **all** of a VM's
`Subdomain` rows as part of the same teardown that releases its `/128` — so a row
never outlives its VM's address (the case the old address-drift sweeper guarded is
closed structurally, which is why the routing model has no sweeper). The **reuse
guard** — `allocate_ipv6` skipping any address still named by a live `Subdomain` — is
the belt-and-suspenders follow-up.

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
  `sync-image.py` exits early ("rootfs already built") when the unpacked rootfs
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

## Developer tooling

Not product, not deferred — present-day scaffolding for anyone working on Atlas
*integration* (the apps layered on top: Central, IAM, billing) who needs Servers,
VMs, and Tasks to look at without standing up real cloud resources.

### Fake provider

`Atlas Settings.provider_type = Fake` (`atlas/atlas/providers/fake.py`) under
which **every action just works** — it transitions Frappe DB state without ever
touching a real host or vendor API. The design point worth recording: faking the
VM lifecycle needs **two** seams intercepted, not one.

1. The `Provider` ABC covers only *server* creation + reserved IPs. `FakeProvider`
   implements it: `provision()` returns a host already `ready` with synthetic,
   *unroutable* networking (IPv4 in TEST-NET-3 `203.0.113.0/24`, IPv6 under
   `2001:db8::/32`, both deterministic per server title); `destroy()` and the
   reserved-IP methods are no-ops; reserved IPs draw from TEST-NET-2
   `198.51.100.0/24`.
2. Every *Virtual Machine* action, image sync, and `Server.bootstrap()` runs as a
   **Task over SSH** through `run_task()`. So `run_task`/`execute_task` consult
   `is_fake_server()` and, for a Fake-backed Server, hand off to
   `atlas/atlas/providers/fake_tasks.py`, which finalizes a `Task` (Pending →
   Running → Success) with no SSH — emitting a valid `ATLAS_RESULT=<json>` for the
   four scripts whose controllers parse one (`bootstrap-server.py`,
   `snapshot-vm.py`, `snapshot-stop-vm.py`, `warm-snapshot-vm.py`).

Routing is **per-Server** (off the Server's own `provider_type`, not the globally
active one), so a Fake provider and a historical real Server coexist and each
Task goes the right way. The real worker still runs (`finish_provisioning` polls
the instant-ready `describe()`, then runs the faked `bootstrap()`), so a Fake
server marches Pending → Active through the real code path.

**Failure injection.** To exercise error paths, name scripts that should fail
either on `Atlas Settings.fake_fail_scripts` (comma/newline list, or `*`) or
per-call via `frappe.flags.fake_fail`. A faked failure is
indistinguishable from a real one to the controller (Task `Failure`, non-zero
exit, `frappe.throw`) — the VM lands `Failed`, the Server lands `Broken`, the
retry button returns.

**Safety.** Every mutating Fake method is gated on `developer_mode`;
`Atlas Settings.provider_type = Fake` on a production site is inert and loud. The
unroutable address blocks mean even an accidental real `ssh` can never reach a
stranger's machine.

The `Atlas Settings._provision_server` controller reads a per-vendor Settings
Single for default size/image; Fake has none, so the lookup falls back to the
dialog values (DigitalOcean/Scaleway are unchanged — their Single exists).

**Desk coverage.** The `fake_provider_desk` e2e
([`atlas/tests/e2e/use_cases/fake_provider_desk.py`](../atlas/tests/e2e/use_cases/fake_provider_desk.py))
is the Fake-provider analogue of `desk_buttons`: it drives *every* operator
button through the exact HTTP layer the desk uses (`run_doc_method` for
controller methods, `execute_cmd` for the Reserved IP module-function buttons),
with the desk's real argument shapes — but against the Fake provider, so it runs
anywhere `developer_mode` is on (e.g. `fake.local`) with no droplet, in seconds.
It is self-contained (sets and tears down its own server/image/VMs,
restores `Atlas Settings.provider_type`) and includes the wrong-state and
fault-injection negatives. Run it directly:
`bench --site fake.local execute atlas.tests.e2e.use_cases.fake_provider_desk.run`.

### Demo / populate script

`atlas/atlas/demo.py` (data tables + builders in `demo_data.py`) stands up a
realistic, varied fleet on the Fake provider:
`bench --site <site> execute atlas.atlas.demo.run` (`--kwargs "{'reset': True}"`
to wipe-and-rebuild). It drives the *real* controllers, so the rows are
internally consistent and the script doubles as a smoke test of the fake seam.
The dataset spans every Server status (Active / Bootstrapping / Broken / Draining
+ a Self-Managed host) and every VM status (Running / Stopped / Paused /
Terminated / Failed) and feature (data disk, stop/termination protection,
memory-snapshot-on-stop, relaxed-CPU burst, proxy with an attached public IPv4),
plus Cold / Warm / Pending / Failed snapshots, Reserved IPs, and back-dated
Tasks. `developer_mode`-gated; idempotent; scoped to Fake providers so a reset
never touches real DO/Scaleway rows.

## Concrete next steps after this iteration

- **Regenerate the jailer launcher on resize**. `resize-vm.py` rewrites
  `firecracker.json` (`vcpu_count` / `mem_size_mib`) and grows the disk LV, but
  does not regenerate the per-VM `jailer-launch.sh`, so a changed cgroup
  `cpu.max` cap (`Virtual Machine.cpu_max_cores`, and the whole-core cap too)
  only takes effect on the next re-provision, not the next Start. Have
  `resize-vm.py` rewrite the launcher's `--cgroup cpu.max=…` line in the same
  gesture (it already has the new values) so a resize applies the bandwidth cap
  immediately. Additive; see [05 § Resize](./05-virtual-machine-lifecycle.md#resize).

- **CPU bursting — let an idle host's spare cycles go to whoever wants them.**
  **Model 2 (hybrid) is now SHIPPED** as the per-VM `cpu_mode` toggle
  ([02 § Virtual Machine](./02-doctypes.md), [`networking.cgroup_args`](../atlas/atlas/networking.py)):
  a `Relaxed` VM gets a `cpu.weight` floor (its `cpu_max_cores` share under
  contention) plus a loose `cpu.max` ceiling at `vcpus` whole cores, so it bursts
  into idle host CPU; `Hard cap` (the default) keeps the original hard ceiling.
  The analysis below is retained for the *remaining* work — model 3 (Fly-style
  accruing balance) and the live-apply follow-up.

  In `Hard cap` mode a VM's `cpu.max` is a *hard* bandwidth ceiling: a `Shared
  1x` (`cpu_max_cores=0.0625` → `cpu.max=6250 100000`) is throttled to 6.25% of a
  core *even when the host is otherwise idle*. CFS bandwidth control is not
  work-conserving — unused cycles are left on the floor, not lent out. This is
  the documented root cause of the slow sub-core boots and the warm-deploy floor
  (throttled boots run minutes; the same clone at full CPU boots in ~11s). The
  shipped `Relaxed` mode fixes exactly this for VMs that opt in.

  The goal: a small VM should be able to **burst into spare host CPU when no one
  else needs it**, while still degrading to its guaranteed share under
  contention. Three models, in increasing fidelity:

  1. **`cpu.weight` (proportional shares).** Drop the quota, or keep it loose,
     and add `--cgroup cpu.weight=<w>` with `w` proportional to the tier
     (`Shared 1x`≈6 … `Dedicated 1x`≈100, basis = `cpu_max_cores`). Now CFS *is*
     work-conserving: a VM gets *at least* its proportional share when the host
     is busy and *all* the idle CPU when it isn't. Cheapest change, but it
     removes the hard ceiling — a single busy VM on an idle host runs flat-out,
     which breaks the "you bought 1/16 of a core" billing story and is the
     opposite of a predictable shared tier.

  2. **Hybrid: `cpu.weight` for fairness + a loose `cpu.max` burst ceiling.**
     ✅ **SHIPPED** as `cpu_mode="Relaxed"`. Weight sets the floor under
     contention; a loose `cpu.max` (here `vcpus` whole cores) caps the burst so
     no VM monopolizes the host or invalidates capacity accounting (which still
     bills the `cpu_max_cores` share). The right default for a multi-tenant
     self-serve product, but the burst is *unconditional* — a VM that has been
     pinned at its ceiling for an hour still bursts, so "burst" becomes "a higher
     hard cap," not "spare cycles you earned by being idle." That last gap is
     what model 3 closes.

  3. **Fly.io's model — quota + an accruing burst balance** (the one the
     operator pointed at: <https://fly.io/docs/machines/cpu-performance/>).
     Fly stays *quota-based* — their "shared CPU = 5ms / 80ms = 6.25%" baseline
     is exactly our `Shared 1x` — but lets a VM **bank unused quota while idle
     and spend it in bursts** (their docs: a 500s balance bursts for ~533s),
     throttling back to baseline only once the balance drains. This is the model
     that matches the operator's actual ask: *"use available CPU if nobody else
     is using it,"* with idleness as the currency.

     The catch is that **cgroup v2 cannot do this on its own.** CFS *does* have
     an accruing-balance knob — `cpu.cfs_burst_us` (v2: `cpu.max.burst`),
     defined as "the maximum accumulated run-time (in microseconds)"
     ([sched-bwc](https://www.kernel.org/doc/Documentation/scheduler/sched-bwc.rst)) —
     but the kernel hard-caps it at **≤ one period's quota** ("any positive value
     no larger than `cpu.cfs_quota_us`"). For `Shared 1x` that buffer is
     `quota = 6250µs` — **6.25ms**: after idling, the VM may spend at most one
     extra period's worth (≈12.5% of a core for a single 100ms window), then it
     is back at the 6.25% wall. Fly's currency is **500 seconds** of balance —
     five orders of magnitude larger. So the kernel buffer smooths a momentary
     spike; it does *not* implement "bank an idle minute, spend it later." Fly's
     balance lives in **their own userspace scheduler on top of CFS**, not in
     the cgroup. To match it Atlas would need an equivalent: a host-side loop
     that watches each VM's `cpu.stat` (`usage_usec` vs. its baseline),
     accumulates a per-VM credit balance, and live-rewrites `cpu.max` between
     "baseline" and "burst ceiling" as the balance fills and drains. That is a
     real on-host agent — squarely against operating principle #5 ("no agent
     runs on the server") — so it is a deliberate, heavier step, not a
     cgroup-flag tweak.

  Two facts constrain all three: (a) the live-apply mechanism already exists —
  the **launcher-regeneration follow-up above** rewrites `--cgroup cpu.max=…` on
  a running jailer, and the H1 warm-unlock work proved a running VM's cgroup file
  can be live-written (see `llm/references/warm-provision-cpu-unlock.md`); (b)
  **a sub-1 tier boots `vcpus=1`**, so it can burst to at most *one* core no
  matter the cgroup setting — multi-core burst would also need `vcpus>1`, which
  changes guest topology and the thread-budget half of capacity accounting
  ([`server_capacity.py`](../atlas/atlas/api/server_capacity.py),
  [`placement.py`](../atlas/atlas/placement.py)). That accounting is the subtle
  blast radius, not the cgroup flag: it sums `cpu_max_cores` as a *bandwidth
  cost* for oversubscription, and any model where VMs routinely exceed that cost
  changes what `overprovision_factor` means.

  Status: the **hybrid (model 2)** is shipped as the `cpu_mode` toggle — it
  delivers "burst into idle CPU" with one additive `cgroup_args` change, no host
  agent, and a ceiling that keeps capacity accounting honest. It defaults to
  `Hard cap` (off), so it is opt-in per VM and capacity accounting is unchanged.
  Remaining work: (i) treat Fly's accruing-balance (model 3) as a later
  refinement only if an *unconditional* burst on an idle host turns out to hurt;
  (ii) regenerate the launcher on resize so a mode/share change applies on the
  next Start, not only on re-provision (the named follow-up above). See
  [02 § Virtual Machine `cpu_mode`](./02-doctypes.md) and
  [05 § Resize](./05-virtual-machine-lifecycle.md#resize).

- **Stuck-task reaper**. A scheduled job that looks at Tasks in `Running`
  state older than 2× their declared timeout and marks them `Failure` with
  a synthetic "worker presumed dead" note. The e2e harness already does
  this via `mark_orphan_tasks_failure`; production needs the same
  guarantee. Pair with the "Server lock doctype" if we ever want
  concurrent-sync protection. Additive.

- **Fast self-serve deploy (sub-5s, no warm pool)**. The post-verify
  `Site.auto_provision` ([14-self-serve.md](./14-self-serve.md)) is dominated
  not by kernel boot (the CoW clone boots in single-digit seconds) but by
  **per-signup work that produces identical output every time** plus
  **poll-loop sleep slop**. Three additive changes remove the fixed cost
  without pre-provisioning idle VMs:
  1. **Bake `bench setup production` into the golden image** so a clone already
     serves on `:80`; the per-signup deploy drops from `rename + full setup
     production` (the rank-1 cost — supervisord + nginx regen, "takes minutes")
     to a light `bench rename-site`. The IPv6-listener fix (`listen
     [::]:80;`) moves to bake time too. (**RESOLVED, then REVISED.** First take:
     "don't rename at all" — keep `site.local`, mark the vhost `default_server`,
     serve any `Host` via `default_site`. Revised: the per-signup deploy now
     **renames** `site.local` → `<fqdn>` via `bench rename-site`, which regenerates
     the vhost as `server_name <fqdn>` + a v6 listener + reload — NO restart. The heavier change that came with it: drop the per-VM
     `set-admin-password` entirely (it cost a ~28s CPU-throttled `bench frappe`
     boot — the real rank-1 deploy cost, not nginx); the owner is handed the shared
     baked password and rotates it. See [14-self-serve.md](./14-self-serve.md)
     Contract A + the in-guest deploy.)
  2. **Tighten the poll intervals** in `_wait_for_vm_running` and
     `wait_for_http` from 5s → 1s (leave the generous *timeouts* alone — only
     the interval, so a legitimately-slow provision still can't spuriously
     `Fail`).
  3. **Bake `deploy-site.py` into the image** (it is already self-contained,
     stdlib-only) so the per-signup path is a single SSH **execute**, not
     `mkdir + scp + execute` — the first entrypoint of an eventual `atlas-guest`
     baked CLI.
  Changes 1 + 3 share one re-bake and repoint `default_bench_snapshot` at the
  new golden (retire the old — keep exactly one). Contracts A/B/C are untouched:
  HTTP-200 stays the single `Running` signal; this only removes slop and fixed
  cost. The **warm pool** of pre-claimed VMs is the *next* lever if these three
  don't hit sub-5s (boot + RQ-pickup are the only residuals they can't remove) —
  explicitly deferred, not built here.

- **Reverse-proxy gaps** ([12-proxy.md](./12-proxy.md)). The proxy itself is
  built and e2e-proven; these are the gaps around it, the first a security gate:
  1. **South-side firewall — scope site `:80` to the proxies** *(security gate)*.
     A site's `:80` is reachable by **anyone** on the v6 internet today (proxies
     are dedicated, not co-located — *Accepted limitations* in 12-proxy). The
     `proxy_vm` e2e proves the proxy *can* reach the site's `:80`; it does **not**
     prove only the proxies can. A per-VM guest firewall scoping inbound `:80` to
     the proxy source addresses — without dropping the proxy hop — does not exist
     yet. This is a release gate, not just a TODO.
  2. **Withdraw an unhealthy proxy from the wildcard.** `upsert_wildcard`
     publishes round-robin A/AAAA over the regional proxy fleet, but there is no
     health signal that *removes* a record when a proxy is down — a dead proxy
     still takes 1/N of the traffic until an operator reconciles by hand.
  3. **Schedule the reconcile loop.** `reconcile_proxy` / `reconcile_region` run
     on demand, but the *periodic* diff (re-`/sync` every proxy on a timer, so a
     rebuilt/drifted proxy self-heals without operator action) is **not wired**:
     `scheduler_events` in [`atlas/hooks.py`](../atlas/hooks.py) carries only the
     daily cert-renewal job. Adding `reconcile_region` there is a trivial step.
  4. **TLS grade (A+) is not automated.** The one image-gate row the compose
     harness can't assert (needs a real cert + `testssl.sh`/`sslyze`). The TLS
     layer now produces a real cert, so this is gradeable — just not yet wired.
  5. **404-only vs 404/503 tombstones.** Shipping 404-only; the known-down `503`
     ("site suspended/preparing") path — a tombstone value in the map — is a small
     additive follow-up for the signup UX.
  6. **Proxy VM sizing.** Per-VM cgroup caps (`vcpus`, `memory_megabytes`) and
     `LimitNOFILE` are at sensible defaults; tune once real load is observed.
  7. **`ssl_certificate_by_lua` / per-subdomain custom-domain certs.** Confirmed
     to work in the self-assembled build; the hook is left in place but unbuilt —
     one wildcard covers everything this iteration. (See also **General tenant
     inbound v4** below, the same shape on the v4 side.)
  8. **Proxy terminate-guard.** A proxy is a terminable VM like any other
     (accepted risk, mitigated by 2–3/region). `Virtual Machine` already carries a
     `termination_protection` flag and guard; *setting* it on proxies is operator
     discipline, not yet automated. A stronger guard (tag / naming convention /
     confirm-dialog) is the additive follow-up if it bites.
  All additive except #1, which is a release gate.

- **General tenant inbound v4.** The v4-attach primitive (`Reserved IP`,
  [06-networking.md](./06-networking.md#ipv4-ingress-reserved-ip)) is gated to
  Atlas-owned VMs; the reverse proxy is its first and only user. Letting a
  dashboard user attach a public v4 to their own VM is a deliberate later step.
  Additive.

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
  - **Existing-VM migration.** VMs provisioned before the jailer change keep
    their old non-jailed unit and flat (non-jail) paths until re-provisioned;
    they are not retro-jailed. The same applies to the **LVM disk swap**: a VM
    whose disk is still a `cp`-copied `rootfs.ext4` *file* is not converted to a
    thin LV in place — the swap is a hard replacement of the disk primitive, not
    a parallel backend. Terminate + re-provision to adopt the jail and the thin
    LV. (On this branch there is no production fleet and the e2e server is
    recreated per run, so nothing is silently broken; this note is for a future
    upgrade of a live host.)

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
  the Atlas Settings **Refresh Catalog** button. Today the images are pinned
  constants (server + minimal noble); this is the additive follow-up.

- **Newer guest release**. Bump the supported guest to Ubuntu 26.04 once it's
  validated as a guest (it boots; the normalization checklist in
  [08-images.md](./08-images.md) is the regression gate). Additive — a new
  image row, same code path.

## Things on the longer-term list

- **Custom images** (`Virtual Machine Image Build`): build an ext4 from a
  Dockerfile or debootstrap recipe, push to a bucket, point the image
  record at it. Additive.

- **LVM thin-pool disks** — *done (v0.6)*. Per-VM disks are LVM thin snapshots
  of a read-only base image LV (`lvcreate -s`), so provisioning and snapshots are
  instant CoW operations sharing blocks until written — the density win the old
  "overlayfs-backed rootfs" item was after, without a doc-type change (LV names
  derive from UUIDs). See [07-filesystem-layout.md § Why LVM thin volumes](./07-filesystem-layout.md#why-lvm-thin-volumes-for-per-vm-disks).
  - **Real attached block-device PV.** The pool sits on a sparse loopback file
    (`pool/atlas-pool.img`) on the root disk because a stock DO droplet has no
    spare block device. A provider that attaches a dedicated volume (DO Block
    Storage, an extra disk) should back the PV with that device instead — a
    one-line change to the `loop_device` assignment in `atlas_pool_ensure`.
  - **Migration via `thin_delta`.** Thin metadata makes an *incremental* disk
    transfer possible (send only changed blocks between two snapshots), the fast
    slice the cross-server-snapshot item below now depends on.
  - **Pool autoscale / quota / GC / drift reconciler.** The pool over-commits;
    today the only guard is the ≥90% `data_percent`/`metadata_percent` pre-flight
    in `snapshot-vm.py`. Autogrowing the pool, per-server/per-team quotas, a
    snapshot reaper, and a reconciler that drops orphan LVs (LV with no matching
    DB row, or vice versa) all belong here before real load.

- **Snapshots** — *done (disk-only)*. Implemented as an instant CoW thin
  snapshot LV (`atlas-snap-<uuid>`, `lvcreate -s` of the VM's disk LV) tracked by
  a `Virtual Machine Snapshot` DocType, with restore/rebuild, clone, resize and
  pause/resume alongside. See
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
    deleted by hand, guarded only by the ≥90% pool-space (`data_percent`/
    `metadata_percent`) pre-flight in `snapshot-vm.py`. A scheduled reaper and
    per-server/per-team pool quotas belong here before any real load (see the
    pool autoscale/quota/GC item under **LVM thin-pool disks** above).
  - **Cross-server snapshots.** A snapshot lives on its VM's server; clone and
    restore target the same server. Moving a snapshot to another host (for
    rebalancing or as an image-build input) is additive but unbuilt.

    It is **not** blocked by the Firecracker cross-host snapshot matrix
    (identical CPU model / host-kernel / GIC version — see
    [snapshot-support.md § "Where can I resume my snapshots?"](../../references/firecracker/docs/snapshotting/snapshot-support.md)).
    Those constraints bind only the serialized *memory-state* snapshot, which we
    deliberately do not use; a disk snapshot is a thin LV whose blocks can be
    streamed to another host (`dd`, or incrementally via `thin_delta`). The real
    blockers are Atlas-side and mundane:
    - **Structural.** A snapshot LV lives in one server's pool and the DocType
      hard-binds `virtual_machine` (`set_only_once`) and a read-only denormalized
      `server`. A transferable snapshot needs a host-independent store and a
      mutable location — a DocType change plus the host→host LV-stream path.
      Largest piece. (The LV is no longer trapped under the VM's directory, so
      the old "dies with the VM dir" coupling is already gone.)
    - **Kernel pairing.** A disk snapshot carries no kernel; clone/restore take
      it from `source_image`. The target host must already have the matching
      `Virtual Machine Image` synced (reuse the `provision-vm.py` step-0
      image-present precondition).
    - **Transfer cost.** The naive slice is a full N-GB block stream (`dd` of the
      snapshot LV over SSH, fail-loud) and is in-grain. The fast slice — send only
      the blocks that differ between two thin snapshots — is unlocked by the
      **migration via `thin_delta`** item under **LVM thin-pool disks** above,
      now that disks are thin LVs rather than independent file copies.
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

    Aside (snapshot security, independent of transfer): guests currently ship
    **no swap** — the per-VM `/swapfile` was dropped from provision. If in-guest
    swap is reintroduced as a `/swapfile` inside `rootfs.ext4`, every disk
    snapshot would capture guest swap contents — a data-remanence concern when a
    snapshot is cloned across a tenant boundary. (The Firecracker prod-host rec
    to disable swap is about *host* swap; this would be the in-guest analogue.)
    Put swap on a separate, non-snapshotted volume, or keep guests swapless, when
    tenancy lands.

- **Health checks**: a scheduled job that runs `systemctl is-active …` per
  VM and reconciles `Virtual Machine.status`. Additive.

- **Metrics**: `firecracker --metrics-path` per VM, shipped to whatever
  metrics store the next layer cares about. Additive.

- **Console access**: signed URL to the serial console via the API socket.
  Needs a small web service. Additive.

- **Quotas / ownership / scheduling**: belongs in the layer above Atlas — that
  layer is now **Central** ([16-central.md](./16-central.md)), which pre-checks
  capability, billing, and quota before driving Atlas. Atlas stays unaware of
  policy: it attributes resources to a `Tenant` ([02-doctypes.md § Tenant](./02-doctypes.md#tenant),
  an attribution-only link today, *not* a `team` field) and enforces only
  physical **capacity** — a create Central authorized but the region can't fit
  is rejected with a typed no-capacity error ([placement.py](../atlas/atlas/placement.py)).

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
- `v0.6` — **LVM thin-pool disks.** Per-VM disks moved from a full `cp` of the
  image rootfs to an instant copy-on-write LVM thin snapshot of a read-only base
  image LV; disk snapshots became thin snapshot LVs too. Bootstrap creates the
  `atlas` VG + `pool0` thin pool on a sparse loopback PV (with reboot survival via
  `atlas-pool.service`); sync imports each base image as a read-only thin LV;
  provision/clone/rebuild `lvcreate -s` off it and `mknod` the LV's block node
  into the jailer chroot (per-VM uid, pure DAC — verified on a real host:
  `DevicePolicy=auto`, no `DeviceAllow`); resize is `lvextend -r`; terminate /
  delete-snapshot `lvremove` (guarded against pool/base LVs). No DocType/schema
  change — LV names derive from UUIDs (`atlas-vm-<uuid>`, `atlas-snap-<uuid>`,
  `atlas-image-<image>`). Verified end-to-end on a DO droplet: a jailed,
  chrooted, de-privileged Firecracker boots off a thin LV. See
  [07-filesystem-layout.md § Why LVM thin volumes](./07-filesystem-layout.md#why-lvm-thin-volumes-for-per-vm-disks).
- `v0.7` — **Reverse proxy.** A TLS-terminating reverse proxy that fronts many
  Frappe sites under a regional wildcard (`*.<region>.frappe.dev`), built as an
  ordinary operator-owned `Virtual Machine` (`is_proxy` + `region`) running a
  self-built nginx + Lua stack ([`proxy/`](../proxy)). The live routing map is a
  `lua_shared_dict` updated by an atomic dict write — **zero nginx reload**. Each
  `Subdomain` row maps one subdomain → one site VM's `/128` (dialed over public
  IPv6 on `:80`); 2–3 proxy VMs per region each hold the whole regional map, and
  Atlas reconciles each proxy's live map over SSH. Inbound v4 **and** v6 on
  `:443` via an attached `public_ipv4` (the Reserved IP primitive). See
  [12-proxy.md](./12-proxy.md).
- `v0.8` — **TLS & domain layer.** The producer for the wildcard cert the proxy
  consumes. Two small registries (DNS, TLS) mirror the compute `Provider` ABC,
  resolved by name. `Root Domain` → **Issue / Renew Certificate** issues the
  regional `*.<region>.frappe.dev` wildcard via Let's Encrypt over a DNS-01
  challenge (`issue-cert.py` runs on the **controller**, a host dependency:
  certbot, certbot-dns-route53, openssl, boto3), pushes the PEMs onto every proxy
  VM in the region (`push_cert`), and publishes the public `*.<domain>` A/AAAA at
  the proxy fleet (`upsert_wildcard`). One `Root Domain` = one region = one
  wildcard. See [13-tls.md](./13-tls.md).
- `v0.9` — **Self-serve sites.** Signup → email-verify → live Frappe site in a
  few-seconds flow, layered on the proxy + TLS halves. Two new user-owned
  DocTypes: `Site Request` (the pre-verification holding row — email + subdomain
  + token; **no** droplet/site work until the email is verified, Contract C) and
  `Site` (the verified user's aggregate, keyed by the **one routing string**
  `<subdomain>.<region domain>` that is at once the proxy Host header and the
  `Site` key — the routing identity, never written on disk; the baked site stays
  `site.local` — Contract A). Fulfilment clones the golden bench
  snapshot (`Atlas Settings.default_bench_snapshot`), runs `deploy-site.py` in the
  guest, and flips the Site to `Running` **only on an observed HTTP 200** from
  `:80` (Contract B), then creates the `Subdomain`. A `/signup` www page + guest
  API is the one guest-reachable surface. See [14-self-serve.md](./14-self-serve.md).
- `v0.10` — **Scaleway Elastic Metal provider.** A third `provider_type`
  alongside DigitalOcean and Self-Managed — bare-metal hosts via the Scaleway
  Elastic Metal API. `Scaleway Settings` (Single) holds the IAM secret key,
  project id, zone, billing mode, and default size/image; `ScalewayProvider`
  (`atlas/atlas/providers/scaleway.py`) wraps a raw-`requests` client
  (`atlas/atlas/scaleway.py`) implementing all ten provider methods (server
  lifecycle + Flexible-IP reserved-IP). Three things differ from DO, all already
  fit by the abstraction: provision is **async** (create returns `delivering`;
  the worker polls `describe()` until `ready` + install `completed`, with a
  per-provider `ready_timeout_seconds` of 3600 and a `ProviderError` that
  short-circuits a terminal vendor state to `Broken`); the **routed `/64`** for
  VMs is a (free) **flexible IPv6** the provider allocates + attaches at provision
  — the *bundled* subnet is on-link SLAAC, not routed — reported whole as
  `ipv6_virtual_machine_range` (no `/124` carve, retiring the 15-VM ceiling);
  inbound IPv4 is a **routed Flexible IP** (the already-built
  `apply_routed_reserved_ip_nat` path — DNAT the IP itself, no anchor). The
  Scaleway Ubuntu image blocks root SSH, so a `Provider.prepare_host` hook does a
  one-shot `ubuntu`→root first-contact before the bootstrap, leaving the rest of
  Atlas's root-SSH layer unchanged. `discover()` hits the API (the per-zone
  `offer_id`/`os_id` UUIDs and prices live nowhere else) and stashes the UUIDs in
  the catalog rows. Controller + client are fully unit-tested offline; the
  **pure-L3 IPv6-to-guest (NDP-proxy, no Virtual MAC)** go/no-go and the
  Flexible-IP inbound-v4 path are proven by the `scaleway_provisioning` e2e on a
  real EM-A610R-NVME box. The thin pool backs on the real NVMe device(s) on
  bare metal (see [07-filesystem-layout.md](./07-filesystem-layout.md)), with a
  loopback fallback for droplets.
