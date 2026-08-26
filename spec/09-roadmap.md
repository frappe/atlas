# Roadmap and deferred decisions

This iteration is a building block. Two flavors of deferral live here:

1. Things we know we'll do later, with a cheap path to add them.
2. Architectural questions we punted on, and how we plan to revisit them.

This chapter is a *deferred-work list*. Design essays for features that have
since shipped have moved to their own chapters; a pointer to each is in
**[Shipped since](#shipped-since-was-on-this-list)** at the end. New deferred
items are added here as short entries — *what* and *why it's deferred* — not as
design essays.

## Punted decisions

### Declarative execution / bootstrap layer — grow our own, don't take pyinfra

The Task model is "one Task = one typed script" ([04-tasks.md](./04-tasks.md),
principle #3) and bootstrap is a single script. Both are the smallest thing that
works: a VM provisions in one round-trip with a single audit row per operation,
trading step-level error attribution for speed. The shape we'd eventually want is
[`pyinfra`](https://pyinfra.com)-like — declare desired state in Python, get
batched per-host commands and structured output — but we won't take the
dependency (it's a large framework assuming a deploy-from-CLI workflow; principle
#6 says copy the small subset, don't import). Grow a ~200-line operations layer
ourselves.

- **When to revisit (scripts):** more than ~3 scripts share large "ensure file /
  ensure package" blocks. Extract a tiny idempotent operations layer; keep the
  Task-per-script contract.
- **When to revisit (bootstrap):** two genuinely different bootstrap paths
  (compute / edge / builder roles), or an operator wants a small surgical change
  to a running server without re-running the whole script.

### Address reuse on archive

Archived VMs hold their IPv6 address forever; new VMs always draw a fresh address.
On a `/124` (15 usable) this caps lifetime VMs per server at 15. (Scaleway's
routed `/64`, v0.10, already retires the ceiling there.) Fix: larger routed
subnets per server, or address reuse with a quarantine window (a "used by VM X
on dates …" lookup in the Task audit).

Related narrower guard, deferred for **subdomain routing**
([18-bench-self-routing.md](./18-bench-self-routing.md) Component F): `allocate_ipv6`
should skip any `/128` still named by a live `Subdomain`, so a stale route can't
briefly point at a recycled address (cross-tenant leak). The v1 mitigation is
structural — `VirtualMachine.terminate()` deletes all of a VM's `Subdomain` rows
in the same teardown that releases its `/128`, so a row never outlives its
address (which is why the routing model needs no sweeper). The reuse guard is the
belt-and-suspenders follow-up.

### Host-key trust / host-key pinning

We use `StrictHostKeyChecking=accept-new` (trust-on-first-use). A compromised
DigitalOcean control plane could swap a droplet between bootstrap and first SSH.
Fix: capture the host key during `Server.provision()` (serial console, or read
`/etc/ssh/ssh_host_ed25519_key.pub` over the first SSH and pin it) into a new
`Server` field. Additive, not breaking.

## Near-term hedges

Cheap structural changes worth making before any production load — much cheaper
to set up early than to retrofit. None change current behavior.

- **Secret indirection for SSH keys and provider tokens.** Keep the fields on
  `Atlas Settings` / per-vendor Settings, but route reads through one helper so
  the storage backend can be swapped to an external secret store without touching
  callers. DB-as-keystore is fine for the PoC, not once customers exist.
- **Spill Task `stdout`/`stderr` over N KB to a file**, keeping a capped excerpt
  + pointer on the Task row, so the DocType doesn't become a log store.
- **Key the image-sync short-circuit to guest content, not just the rootfs.**
  `sync-image.py` exits early when the unpacked rootfs is present, but the baked-in
  guest systemd unit (`scripts/guest/atlas-network.service`) is invisible to an
  already-synced server until the rootfs is rebuilt for some other reason. Today
  the escape hatch is the immutable-image contract (any change to a spec image
  field forces a rebuild via `ensure_image_row()` delete-and-reinsert). The fix is
  to stamp a content digest of the guest payload into the image row and key the
  short-circuit on it. Additive.

## Deferred work

### Compute / capacity

- **CPU bursting — model 3 (accruing balance).** Model 2 (hybrid `cpu.weight`
  floor + loose `cpu.max` burst ceiling) shipped as the per-VM `cpu_mode="Relaxed"`
  toggle ([02-doctypes.md](./02-doctypes.md), `networking.cgroup_args`); its burst
  is *unconditional*. Fly.io's model banks unused quota while idle and spends it
  in bursts — "use spare CPU, with idleness as the currency." cgroup v2 **cannot**
  do this alone: `cpu.max.burst` is hard-capped at one period's quota (6.25 ms for
  `Shared 1x`), five orders of magnitude short of Fly's ~500 s balance. Matching it
  needs a host-side loop that watches each VM's `cpu.stat`, accrues a per-VM credit
  balance, and live-rewrites `cpu.max` between baseline and burst — a real on-host
  agent, squarely against principle #5 ("no agent runs on the server"). A
  deliberate, heavier step; do it only if unconditional burst turns out to hurt.
  Also note a sub-1 tier boots `vcpus=1`, so multi-core burst additionally needs
  `vcpus>1`, which changes guest topology and the thread-budget half of capacity
  accounting ([placement.py](../atlas/atlas/placement.py) — the subtle blast
  radius, not the cgroup flag).
- **Server lock doctype.** A single-row lock keyed by `(server, resource)` that
  long-running mutating Tasks (sync-image, provision, migration) take before work.
  Two concurrent syncs of the same image-on-server are a benign bandwidth race
  today; with more operators it stops being benign. Additive.
- **Stuck-task reaper.** A scheduled job that fails Tasks stuck `Running` past 2×
  their timeout with a synthetic "worker presumed dead" note. The e2e harness does
  this via `mark_orphan_tasks_failure`; production needs the same guarantee.
- **Health checks.** A scheduled per-VM `systemctl is-active` reconcile of
  `Virtual Machine.status`. Partly arriving already: the Boat observed-state mirror
  ([33-boat.md](./33-boat.md)) pulls per-VM observed fields; a dedicated reconciler
  is still additive.
- **Console access.** Signed URL to the serial console via the API socket. Needs a
  small web service. Additive. (Distinct from the shipped `SSH Console`, which is
  ad-hoc SSH command fan-out, not serial console.)

### Isolation hardening (jailer is shipped — [05](./05-virtual-machine-lifecycle.md), [06](./06-networking.md), [07](./07-filesystem-layout.md))

- **Unprivileged SSH transport.** Atlas still connects to the host as `root`. The
  scripts already call `sudo` explicitly (a no-op as root), so moving to an `atlas`
  user with a narrow `sudo` allowlist is "create the user," not "rewrite every
  script" — it touches the SSH connection layer (which user the key authenticates
  as). This is the remaining root surface after the jailer. **AppArmor profile**
  (Firecracker ships one meant to run *with* the jailer) pairs naturally with this
  move. Additive.
- **CPU pinning.** We cap CPU *bandwidth* (`cpu.max`), not affinity.
  `cpuset.cpus`/`cpuset.mems`/NUMA needs host-topology modeling we don't do yet.
- **New PID namespace per VM** (`--new-pid-ns`), **custom seccomp filters**, and
  **block/net rate limiters** — extra isolation/tuning knobs on the jailer +
  Firecracker defaults. Additive.
- **Existing-VM migration to the jail + thin LV.** VMs provisioned before the
  jailer / LVM-thin change keep their old non-jailed unit, flat paths, and
  `cp`-copied `rootfs.ext4` file until re-provisioned; they are not converted in
  place. Terminate + re-provision to adopt them. (No production fleet on this
  branch, so nothing is silently broken; this is a note for a future live-host
  upgrade.)
- **More host hardening** (deferred from the host-hardening iteration): `/tmp` and
  `/dev/shm` mount options (`nodev,nosuid,noexec` — awkward where `/tmp` isn't a
  separate mount), `auditd` with a tuned ruleset (real log volume), and
  **surfacing "reboot pending"** after an unattended security-kernel update (we
  deliberately do *not* auto-reboot — it would kill running VMs — so a health
  check should flag hosts needing an operator-scheduled reboot). All additive.

### Storage / snapshots (LVM thin pool + disk snapshots are shipped — [07](./07-filesystem-layout.md), [05](./05-virtual-machine-lifecycle.md))

- **Real attached block-device PV.** The thin pool sits on a sparse loopback file
  on the root disk because a stock DO droplet has no spare block device. A provider
  that attaches a dedicated volume should back the PV with it — a one-line change to
  `loop_device` in `atlas_pool_ensure`. (Scaleway bare metal already backs on real
  NVMe; loopback is the droplet fallback.)
- **Pool autoscale / quota / GC / drift reconciler.** The pool over-commits; the
  only guard today is the ≥90% `data_percent`/`metadata_percent` pre-flight in
  `snapshot-vm.py`. Autogrow, per-server/per-team quotas, a snapshot reaper, and a
  reconciler dropping orphan LVs (LV with no DB row, or vice versa) belong here
  before real load.
- **Operator-facing memory-state snapshots / live clones.** Firecracker
  memory-state snapshots ship for exactly one internal purpose — the opt-in fast
  stop/start and sleepy-VM resume on the *same host* ([32-sleepy-vms.md](./32-sleepy-vms.md)).
  Making them operator-facing (resume a running VM with RAM, true live clones)
  needs a forked boot path (load is pre-boot-only, incompatible with
  `--config-file`), a lifetime RAM-sized memory file, and a guest identity-rotation
  story for the duplicate-state hazard. Out of scope until there's a concrete need.
- **Snapshot retention / GC / quotas** — same pool-space guard as above; a
  scheduled reaper + per-server/per-team quotas before real load.
- **Migration via `thin_delta`.** Thin metadata makes an *incremental* disk
  transfer possible (send only blocks changed between two snapshots) — the fast
  slice the cross-server-snapshot item below depends on.
- **Cross-server snapshots.** A snapshot lives on its VM's server; clone/restore
  target the same server. Moving one to another host (rebalancing, or as a
  build input) is *not* blocked by the Firecracker cross-host memory-snapshot
  matrix — we don't use serialized memory snapshots; a disk snapshot is a thin LV
  whose blocks can be streamed (`dd`, or incrementally via `thin_delta`). The real
  blockers are Atlas-side:
  - **Structural (largest):** the LV lives in one server's pool and the DocType
    hard-binds `virtual_machine` (`set_only_once`) + a read-only denormalized
    `server`. A transferable snapshot needs a host-independent store, a mutable
    location, and a host→host LV-stream path.
  - **Kernel pairing:** a disk snapshot carries no kernel; the target host must
    already have the matching `Virtual Machine Image` synced.
  - **Transfer cost:** naive slice is a full N-GB stream; the fast slice is the
    `thin_delta` item above.
  - **Trust boundary:** Firecracker only CRCs snapshot files and requires
    auth+encryption for host→host movement. Atlas has no host↔host trust and no
    at-rest rootfs encryption — both are gaps before a customer-facing transfer.
  - **Networking:** `ipv6_address` is per-server, so a transferred snapshot can
    only feed a **clone** on the target (fresh identity/IP) — unblocked today. Same
    VM / same IP / new host (VM mobility, e.g. draining a host) additionally needs
    the **floating-IP** backlog idea as a hard predecessor.
  - **Operations:** a multi-minute two-host Task wants the **Server lock** and
    **stuck-task reaper** above before real load.

  *Snapshot security aside (independent of transfer):* guests ship **no swap**. If
  an in-guest `/swapfile` is reintroduced inside `rootfs.ext4`, every disk snapshot
  captures guest swap — a data-remanence concern when a snapshot is cloned across a
  tenant boundary. Put swap on a separate non-snapshotted volume, or keep guests
  swapless, when tenancy lands.

### Reverse proxy ([12-proxy.md](./12-proxy.md)) — the proxy is built and e2e-proven; these are the gaps around it

1. **South-side firewall — scope site `:80` to the proxies *(security gate, not
   just a TODO)*.** A site's `:80` is reachable by anyone on the v6 internet today
   (proxies aren't co-located with sites). The `proxy_vm` e2e proves the proxy
   *can* reach the site, not that *only* it can. A per-VM guest firewall scoping
   inbound `:80` to the proxy source addresses — without dropping the proxy hop —
   does not exist yet. Release gate.
2. **Withdraw an unhealthy proxy from the wildcard.** `upsert_wildcard` publishes
   round-robin A/AAAA over the regional fleet, but no health signal *removes* a
   record when a proxy is down — a dead proxy still takes 1/N of traffic until an
   operator reconciles by hand.
3. **Schedule the proxy reconcile loop.** `reconcile_proxy` / `reconcile_region`
   run on demand; the *periodic* re-`/sync` (so a rebuilt/drifted proxy self-heals)
   is not wired into `scheduler_events`. Trivial to add. (The scheduler already
   runs many jobs — cert renewal, migration/export/sleep reconcilers, the Boat
   mirror pull — proxy reconcile just isn't one of them yet.)
4. **Automate the TLS grade (A+) check.** Now that the TLS layer produces a real
   cert, the grade is testable (needs `testssl.sh`/`sslyze`) — just not wired.
5. **404/503 tombstones.** Shipping 404-only; the known-down `503`
   ("suspended/preparing") tombstone in the map is a small additive signup-UX
   follow-up.
6. **Proxy VM sizing.** Per-VM cgroup caps and `LimitNOFILE` are at sensible
   defaults; tune once real load is observed.
7. **`ssl_certificate_by_lua` / per-subdomain custom-domain certs.** Confirmed to
   work; the hook is in place but unbuilt — one wildcard covers everything this
   iteration. (Same shape as **general tenant inbound v4** below on the v4 side.)
8. **Proxy terminate-guard.** A proxy is a terminable VM like any other (accepted
   risk, mitigated by 2–3/region). `termination_protection` exists; *setting* it on
   proxies is operator discipline, not yet automated.

All additive except #1, the release gate.

### Networking / provider

- **General tenant inbound v4.** The v4-attach primitive (`Reserved IP`,
  [06-networking.md](./06-networking.md#ipv4-ingress-reserved-ip)) is gated to
  Atlas-owned VMs; the reverse proxy is its only user. Letting a dashboard user
  attach a public v4 to their own VM is a deliberate later step. Additive.
- **Warm pool of pre-claimed VMs.** The sub-5s self-serve fixed-cost work shipped
  ([14-self-serve.md](./14-self-serve.md): baked `bench setup production`,
  `rename-site` deploy, no per-VM admin reset, baked `deploy-site.py`). A warm pool
  of pre-provisioned idle VMs is the *next* lever if boot + RQ-pickup residuals
  don't hit sub-5s — explicitly deferred, not built.

### Catalog / CLI / arch

- **CLI grammar (Phase 2) + REST CLI.** The host `atlas` CLI ships in Phase 1 with
  script stems as verbs ([03-bootstrapping.md](./03-bootstrapping.md)). Phase 2
  reshapes that into a verb/noun grammar (`atlas vm stop`) over the same dispatch
  and extends it to controller-only scripts. A separate, thinner **REST CLI** calls
  Frappe's REST API from an operator's laptop (the button DocType-methods become
  its commands). Both additive, done as their own changes.
- **Multi-arch (`aarch64`).** Drop the `ARCHITECTURE` hard-coding; the Ubuntu
  cloud archive publishes arm64 squashfs + kernels. Additive on `Server` + the
  image record.
- **Ubuntu image discovery.** A "Refresh Ubuntu Images" action scraping
  `cloud-images.ubuntu.com` (+ `SHA256SUMS`) into a catalog, mirroring
  `provider.discover()` / the **Refresh Catalog** button, so operators pick a
  release × variant instead of hand-copying `DEFAULT_IMAGE`/`MINIMAL_IMAGE`
  constants. Additive.
- **Newer guest release.** Bump the supported guest to Ubuntu 26.04 once validated
  (the [08-images.md](./08-images.md) normalization checklist is the regression
  gate). Additive — a new image row, same code path.
- **Arbitrary base-image build (Dockerfile / debootstrap).** The operator image
  builder shipped ([15-image-builder.md](./15-image-builder.md)) as bake-a-VM →
  snapshot → optional register/promote. Building an ext4 directly from a Dockerfile
  or debootstrap recipe (no scratch VM) is a different mechanism, still unbuilt.
  Additive.

## Developer tooling (shipped scaffolding, not deferred)

Present-day scaffolding for anyone building the layers on top (Central, IAM,
billing) who needs Servers, VMs, and Tasks without real cloud resources. All
`developer_mode`-gated and inert on production.

- **Fake provider** (`Atlas Settings.provider_type = Fake`,
  `atlas/atlas/core/providers/fake.py`): every action transitions Frappe state
  with no host/vendor call. Two seams are faked — the `Provider` ABC (synthetic
  *unroutable* addresses so an accidental `ssh` can never reach a stranger:
  TEST-NET-3/`2001:db8::`, reserved IPs from TEST-NET-2) and every VM/image/bootstrap
  Task via `fake_tasks.py` (finalizes a Task with a valid `ATLAS_RESULT`). Routing
  is per-Server, so Fake and real Servers coexist. Failure injection via
  `fake_fail_scripts` / `frappe.flags.fake_fail`. Covered by the
  `fake_provider_desk` e2e.
- **Demo / populate script** (`atlas/atlas/demo.py` + `demo_data.py`):
  `bench --site <site> execute atlas.atlas.demo.run` stands up a varied fleet on
  the Fake provider through the *real* controllers (so it doubles as a fake-seam
  smoke test); `--kwargs "{'reset': True}"` wipes and rebuilds. Idempotent, scoped
  to Fake providers.

## Policy lives above Atlas, not here

**Quotas / ownership / scheduling** belong to the layer above Atlas — now
**Central** ([16-central.md](./16-central.md)), which pre-checks capability,
billing, and quota before driving Atlas. Atlas stays policy-unaware: it attributes
resources to a `Tenant` ([02-doctypes.md § Tenant](./02-doctypes.md#tenant), an
attribution-only link, *not* a `team` field) and enforces only physical
**capacity** — a create Central authorized but the region can't fit is rejected
with a typed no-capacity error ([placement.py](../atlas/atlas/placement.py),
[28-placement.md](./28-placement.md)).

## Things we will not do, regardless

- Build our own hypervisor.
- Build a portal. Desk and a future CLI cover what we need.
- Adopt Kubernetes.
- Multi-tenant secrets management in this app.

## Shipped since (was on this list)

Items that were future/next-step here and have since shipped — design now lives in
the chapter, not this roadmap:

- **Jailer** — every Firecracker process runs de-privileged, chrooted, with per-VM
  cgroup-v2 caps and its own netns. ([05](./05-virtual-machine-lifecycle.md),
  [06](./06-networking.md), [07](./07-filesystem-layout.md))
- **LVM thin-pool disks** (v0.6) and **disk snapshots** — instant CoW thin
  snapshots. ([07](./07-filesystem-layout.md), [05](./05-virtual-machine-lifecycle.md),
  [02](./02-doctypes.md))
- **CPU bursting model 2** — the `cpu_mode="Relaxed"` toggle. ([02](./02-doctypes.md))
- **Regenerate the jailer launcher on resize** — `resize-vm.py` now rewrites the
  launcher's `--cgroup` lines (memory + cpu) so a resize/mode change applies on the
  next Start, not only on re-provision. ([05 § Resize](./05-virtual-machine-lifecycle.md#resize))
- **Metrics** — host + per-VM time-series pushed to frappe/datum by the resident
  Boat daemon. ([34-metrics.md](./34-metrics.md))
- **Fast self-serve deploy** (fixed-cost removal: baked production, `rename-site`,
  no per-VM admin reset, baked `deploy-site.py`). ([14-self-serve.md](./14-self-serve.md))
- **Operator image builder** — bake → snapshot → register/promote. ([15-image-builder.md](./15-image-builder.md))
- **Load-aware placement**, **Central front door**, **sleepy VMs**, **VM
  migration**, **the WireGuard mesh + ANCP**, and **the Boat daemon** all shipped
  — see the [README table of contents](./README.md).

## Changes

- `v0.1` — initial spec.
- `v0.2` — renamed `Metal Node`→`Server`, `Metal Command`→`Task`, `VM
  Image`→`Virtual Machine Image`; system `ssh` over paramiko; one Task = one
  script; Firecracker v1.15.1; DO `/124` routing constraint; VMs are UUIDs and
  keep their name on archive; scripts live in `atlas/scripts/`.
- `v0.3` — `Self-Managed` provider type; `Provision Server` takes IPv4/IPv6 for
  self-managed hosts; any `ipv6_virtual_machine_range` prefix length accepted.
- `v0.4` — IPv4 egress via host NAT44 (per-VM private `/30`, host masquerade;
  egress-only). See [06-networking.md](./06-networking.md).
- `v0.5` — host hardening at bootstrap (CIS sysctls, sshd drop-in, module
  blocklist, unattended updates, KSM/swap off) as portable `*.d` drop-ins.
- `v0.6` — LVM thin-pool disks (instant CoW; no schema change). See
  [07-filesystem-layout.md](./07-filesystem-layout.md).
- `v0.7` — reverse proxy (`is_proxy`, nginx+Lua, `lua_shared_dict` map, zero
  reload). See [12-proxy.md](./12-proxy.md).
- `v0.8` — TLS & domain layer (DNS/TLS registries; `Root Domain` → wildcard via
  LE DNS-01). See [13-tls.md](./13-tls.md).
- `v0.9` — self-serve sites (`Site Request` / `Site`; deploy → HTTP-200 →
  `Subdomain`). See [14-self-serve.md](./14-self-serve.md).
- `v0.10` — Scaleway Elastic Metal provider (third `provider_type`; async
  provision, routed `/64`, Flexible-IP inbound v4). See providers/scaleway.
- *Later versions* — proxy TCP layer, bench self-routing, VPN broker + customer
  gateway, per-VM firewall, management tunnel, observability, supply chain, VM
  migration, private mesh + ANCP, placement, snapshot-to-S3, sleepy VMs, Boat, and
  metrics all landed after v0.10; see the [README](./README.md) table of contents
  for each.
