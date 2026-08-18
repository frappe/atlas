# Virtual-machine migration between hosts

> **Status: BUILT** (change-address + per-VM keep-address forward). The
> controller-side phase machine is `atlas/atlas/core/migration.py` (+
> `migration_forward.py`, `migration_preflight.py`, `migration_layout.py`); the host
> work runs as **Boat migrate phases** (spec/33), not raw SSH. **Not built:**
> Reserved-IP reassign-preserve (§6 — built behaviour is detach-and-drop), and the
> Scaleway `/64` **range-move** (§2.1–§2.7 — superseded by the per-VM forward and
> never built; kept below as rejected-alternative history).

Migration moves a **Stopped** virtual machine's disk(s) from a **source** server to
a **target** server, keeping the VM's identity (its UUID and everything derived from
it), with minimal downtime. It is **cold** migration: the guest is shut down during
cutover, and the guest's **RAM never crosses hosts** — a [non-goal](./README.md) and
a hard Firecracker constraint (a memory-state snapshot only restores on a matching
CPU model / host kernel / Firecracker build; see
[05 § Warm snapshot fan-out](./05-virtual-machine-lifecycle.md#warm-snapshot-fan-out-one-golden-n-restored-clones)).
Only the **disk** moves.

The disk moves over **NBD** (the source exports a crash-consistent LVM thin snapshot
of the VM's disk LV) into a device-mapper **`clone`** target on the destination: the
target VM **boots immediately**, reading through to the source over NBD while a
background *hydration* copies every block locally; once hydration is 100% the
dm-clone is collapsed to the plain thin LV and the NBD export is torn down. (Built
transport for the disk stream: plain TCP to the source's public IPv4 — an encrypted
host-to-host carrier is a deferred hardening, §2.1.)

The whole operation is a **resumable phase state machine** on a
`Virtual Machine Migration` row, self-driven by `start_migration` and backstopped by
the scheduled `reconcile_migrations` callback — so it survives a provider
rate-limit, a dropped RQ job, or an SSH blip. Each phase is idempotent and is
re-entered from the row's recorded `status`.

## 0. Minimal-downtime cutover (boot-then-hydrate) — BUILT

The guest's downtime — the window between the source unit stopping and the target
unit starting — is minimized by **booting the guest on the dm-clone read-through
before hydration**, so the block copy overlaps *uptime* instead of downtime. The
built `PHASE_ORDER` (`migration.py`) is:

```
Pending           stop the source VM                              ← DOWNTIME STARTS
ExportingSnapshot source snapshot + qemu-nbd (disk frozen)
TargetPreparing   create LV(s) + nbd-client + dm-clone
InjectingIdentity allocate address (change-address) + inject identity THROUGH the clone
CutoverStarting   provision-vm boots the target on the dm-clone read-through  ← VM UP, DOWNTIME ENDS
Hydrating         poll to 100% while the guest runs, reading through NBD      ← off the downtime clock
CollapseClone     reload the dm table clone→linear once 100% (see §5)
Repointing        commit VM row / re-point Subdomains (change-address)
Cleanup           source teardown
```

Downtime is stop + export + prepare + inject + (keep-address: target-receive +
source-forward) + boot; hydration and collapse are off it. The pre-boot phases run
inline in one self-driven worker job (`resumable.self_drive`), no scheduler tick
between them; the multi-minute copy self-paces on the inline hydration poll.

**The consistency invariant that makes this safe.** The source VM's disk **must stay
frozen** (no writer) for the whole window the target reads through it. Cold
migration gets this for free by keeping the source **Stopped from `Pending` until
`Cleanup`**: with the source off there is **no writer racing the reader**, so the
read-through can never see a torn write — the crucial difference from *live*
migration (a [non-goal](./README.md)). Rollback stays trivial through `Hydrating`:
`dmsetup remove` the target clone and `systemctl start` the source; its disk was
never mutated.

**Two host facts this rests on (both host-verified 2026-07-02, now built):**

- **Identity is injected THROUGH the clone device**,
  `/dev/mapper/atlas-vm-<uuid>-clone`, not the bare `atlas-vm-<uuid>` thin LV (held
  open by the clone, mounts `busy`). Writes through the clone land on the dest and
  count toward hydration. `provision-vm` at boot does **not** re-inject.
- **Collapse re-points a live guest's disk without a stop.** `CollapseClone`
  **reloads the dm-clone table from `clone` to a `linear` map** onto the plain dest
  LV (suspend → reload → resume), keeping the **same major:minor**, so Firecracker's
  open rootfs fd survives. `dmsetup remove` on that open fd fails "Device or resource
  busy"; reload-to-linear does not. (See §5 *Collapse*.)

*Remaining future trim (not built):* run the source export and the target-prepare
setup concurrently — they touch different hosts and only the nbd dial must wait.

## Why this shape

Three decisions dominate.

### 1. The UUID and the IPv6 `/128` are both preserved; only `server` changes

A `Virtual Machine.name` is a UUID, immutable forever, and **everything host-local
is derived from it** — MAC, TAP, netns, per-VM uid, veth pair (all pure functions of
the UUID; see [06](./06-networking.md), `networking.py`). The target host re-derives
them **identically**, so there is no collision (the source's are torn down), and the
VM keeps its SSH host keys, history, and links.

`server` is in `IMMUTABLE_AFTER_INSERT` and `validate()` throws on any change
([05 § Why resource fields are frozen](./05-virtual-machine-lifecycle.md#why-resource-fields-are-frozen-outside-resize)).
Migration is the **one sanctioned path** that repoints it, gated by a
`flags.migrating` branch in `validate()` that mirrors the proven `flags.resizing`
pattern (`_finalize_cutover`). The alternative — create a new VM on the target and
terminate the old — was rejected: it burns the UUID, breaks the SSH identity, and
orphans every `Subdomain` (whose `virtual_machine` field is itself immutable).

On the **keep-address** path the VM also keeps its `ipv6_address`, so `server` is the
**only** field that changes. On the **change-address** path (§2.8 fallback)
`ipv6_address` changes too, the same `flags.migrating` gate letting it through.

### 2. The public IPv6 `/128` is preserved and routed across hosts

The `/128` is preserved by **keeping the source host holding its `/64` and
permanently forwarding the `/128` to the target** over a per-VM tunnel (§2.9), for
every keep-address provider. Because the address is unchanged,
`derive_ipv4_link(ipv6)` is unchanged, so the NAT44 `/30`, `VIRTUAL_MACHINE_IPV4*`,
and `/etc/atlas-network.env` are byte-identical — **no network-env re-injection** —
and every `Subdomain.address` (the denormalized VM `/128`) is already correct, so the
**proxy/Subdomain re-point and reconcile are eliminated** on the keep path. They
survive only on the change-address fallback (§2.8, §3).

> An earlier Scaleway-specific design kept the `/128` by moving the whole routed
> `/64` to the target with a provider API call once every VM sharing it had drained
> (§2.1–§2.7). It was **superseded** by the per-VM forward above (which works on
> every provider with no drain and no API move) and **never built**.

### 3. The proxy/Subdomain re-point (change-address path only)

`Subdomain.address` (the denormalized `/128` the proxy dials) is `read_only`,
refreshed from `Virtual Machine.ipv6_address` only inside `_denormalize_address` on
`validate()`; and `Subdomain.on_update` reconciles the fleet **only when `active`
flips** (`subdomain.py`). So neither a stale save nor a quiet field write reaches the
proxy. On the **change-address** path `Repointing` therefore fires the PaaS-blind
`vm.address_changed` callback (`callbacks.run`, `migration.py`) — the services layer
registers the handler that does both halves: write `Subdomain.address` for every
Subdomain whose `virtual_machine` is this VM (the write the read-only/validate path
won't do), then reconcile the proxy for each region touched (the push the
`on_update` hook won't fire on its own). On the **keep-address** path neither
happens: the address is unchanged, the rows are already correct, and the callback is
not fired.

> **Region.** Each Atlas instance operates in **one region**, so a migration's source
> and target are always same-region by construction — no cross-region case, and
> `Subdomain.region` and the Reserved-IP region binding are satisfied trivially. The
> VM's `region` is copied from the source verbatim.

## 2. The IPv6 `/128` cross-host routing (keep-address path)

Keep-address is **one mechanism for every provider**: the source host permanently
forwards each migrated VM's `/128` to the target over a per-VM tunnel. Whether a
migration keeps the address is decided by a single Provider **capability** method,
`vm_range_is_forwardable` (§2.8), never a `provider == "..."` literal.

> **Superseded, never built (§2.1–§2.7 below).** The original design was
> Scaleway-specific and moved the whole routed `/64` to the target once a source
> drained (a "range-move"). It was replaced by the per-VM forward (§2.9), generalized
> to all providers — Scaleway included ("we never move the `/64`, only forward the
> individual `/128`", `providers/scaleway.py`). §2.1–§2.3 survive because the per-VM
> forward **reuses** the tunnel + route mechanics; §2.4–§2.7's block-drain /
> `/64`-move / block-Server-fields are the abandoned parts and are reduced to notes.

### 2.0 The two transit facts the design is built on

Two host-verified provider facts dictate the shape of the forward and correct the
naive "brief inbound-only forward" framing:

1. **Delivery needs proxy-NDP — the re-assert at cutover is load-bearing, every
   provider (host-verified 2026-07-02).** The upstream switch delivers a `/128` to a
   host **only while the host answers Neighbor Solicitations for it on the uplink** —
   exactly what `vm-network-up.py` does unconditionally at provision, Scaleway
   included. There is **no on-link `/64` route** to lean on. Field proof: after a
   keep-address cutover the tunnel was healthy and the source could ping the VM, but
   inbound saw **100% loss** and `ip -6 neigh show proxy` was empty; the instant the
   proxy-NDP entry was re-added, loss went **100% → 0%**. So the keep-address path
   **must re-assert proxy-NDP on the source at cutover for every provider**
   (`_install_forward_routes` passes `REASSERT_PROXY_NDP=1` unconditionally), and
   Collapse-forward must deassert it (symmetry). The row's `forward_address` flag is
   demoted to provider metadata and **no longer gates** NDP.

   > **Verification gotcha.** The edge caches a resolved neighbor for a **long** time
   > (>6 min observed), so *deleting* the entry on an already-resolved `/128` does not
   > drop delivery within a several-minute window — a delete-and-wait test
   > misleadingly reads "NDP doesn't matter." The behaviour only shows on the **add**
   > direction against a *broken* address, or after the full edge-cache TTL.

2. **Egress is source-address-validated at the switch (host-verified 2026-07-02).** A
   box sourcing an address it doesn't legitimately hold is **dropped in the fabric** —
   verified stronger than BCP38-by-prefix: packets from a freshly-added `/128` (even
   one from the host's own on-link prefix) are dropped, while packets from the SLAAC
   address arrive. **Host-side `ip addr add` is NOT a valid egress claim.** Therefore
   the **conservative branch is the only correct one**: during the forward window the
   `/64` stays on the **source**, so only the source may egress the VM's `/128`. The
   tunnel is **bidirectional and full-bandwidth** — inbound lands at the source and
   goes down the tunnel to the target; the guest's replies come back **up the tunnel**
   to the source and egress there (§2.3/§2.9.3's return route). Asymmetric return (the
   target egressing directly) **does not work** — confirmed, not assumed.

### 2.1 The tunnel — a per-VM TUN link over a socat-bridged TCP stream

Hosts have **no private fabric** ([06](./06-networking.md)). The forward carrier is a
**`tun` device per VM, bridged to a forwarded TCP stream by `socat`** — TUN (not TAP)
because we carry exactly one L3 family (the inner IPv6 `/128`); the v4 NAT44 `/30` is
host-local egress and never crosses. No new firewall port and no proto-41/47 hole.

**Naming — keyed to the VM's UUID.** Device name `mig6-<first 8 hex of the VM UUID>`
(≤15 chars, IFNAMSIZ-safe, same discipline as `derive_tap`); both hosts derive it
identically. Pure helpers in `networking.py`: `derive_vm_tunnel` (iface),
`derive_vm_tunnel_port` (per-VM localhost port), `derive_vm_tunnel_table` (return
route-table id). Brought up on both ends in `TargetPreparing`
(`migration._bring_up_forward_tunnel` → Boat `migration-forward-up`, idempotent):
source runs the socat TCP listener + TUN, target dials it.
`net.ipv6.conf.all.forwarding=1` (set at bootstrap) lets each host forward between
the TUN and the per-VM veth.

> **Open for the operator: TUN-over-socat throughput/MTU under live load.** The
> tunnel carries real bidirectional customer traffic for the whole forward window.
> Pin the TUN MTU to the IPv6 minimum (`mtu 1280`) to avoid in-tunnel PMTU surprises,
> and **host-probe throughput before relying on it in production.** The carrier is
> swappable; a real `ip6tnl` fallback reintroduces the public-internet path
> [06](./06-networking.md) forbids, so it stays an operator-gated escape hatch.

> **Superseded naming (range-move).** The abandoned range-move keyed one tunnel to a
> whole draining `/64` (`mig6-<fip8>`, `derive_block_tunnel`); the built per-VM
> forward keys per VM (`mig6-<vm8>`).

### 2.2 Source-side forward — swap the veth route for the tunnel route

After cutover the source's `vm-network-down.py` (`ExecStopPost`) has already torn
down this VM's netns/veth/tap and its delivery route. But the source **still holds
the `/64`**, so inbound for the `/128` still lands there. A typed
`migration-source-forward` Boat phase (`CutoverStarting`, after the source unit is
down) re-establishes reachability onto the tunnel: an **atomic**
`ip -6 route replace <vmv6>/128 dev mig6-<vm8>` (single rtnetlink op, no
delete-then-add black hole), the two `inet atlas forward` nft rules that admit both
directions, and the **proxy-NDP re-assert**. Driven from
`migration._install_forward_routes`.

**Proxy-NDP is load-bearing, not a no-op (§2.0 fact 1).** `vm-network-down.py`
removed this VM's proxy-NDP entry at unit stop, and the edge delivers the `/128` to
the host **only** while it answers NDP — so this phase **must re-assert** the entry or
all public ingress black-holes (100% → 0% loss the instant it was re-added).

### 2.3 Target-side receive — normal `vm-network-up`, plus a return route

The target runs its **normal `vm-network-up.py`** at unit start (same netns/veth/tap,
same delivery route — no change to `vm-network-up.py`). The one addition (a
`migration-target-receive` Boat phase, run **before** the source-forward so the
return path exists first) is the **return-route policy** that forces the guest's
replies back up the tunnel rather than out the target's own (spoof-dropped) uplink: an
`ip -6 rule from <vmv6> lookup <vm_table>` plus a default route via `mig6-<vm8>` in
that table (`<vm_table>` = `derive_vm_tunnel_table`, one rule per migrated VM). This
is the load-bearing fix for the BCP38 egress drop (§2.0 fact 2): inbound arrives
source→tunnel→veth, and outbound is policy-routed veth→tunnel→source→uplink, so every
customer-facing packet is sourced from the box that owns the `/64`.

### 2.4 Block-drain rule — SUPERSEDED, never built

Range-move only: a whole-`/64` drain-to-one-target rule and multi-target pre-flight
throw. Not built. The per-VM forward (§2.9) has no block and no drain — each VM's
tunnel is independent, so there is no "lasting bridge" case to force.

### 2.5 The `/64` move — SUPERSEDED, never built

Range-move only: a deferred reconciler moving the flexible `/64` to the target via a
`Provider.move_flexible_ipv6(fip_id, target)` API pair once the source drained, plus
the `Server.ipv6_virtual_machine_range` rewrites. No such Provider method, reconciler,
or Server-field write exists. The per-VM forward never moves the `/64`; the source
keeps holding it and forwards each `/128` indefinitely.

### 2.6 State-machine integration

The per-VM keep-address phase deltas (vs. the change-address path) are the built
table in **§2.9.4**. There is **no** deferred `reconcile_block_fip_moves` reconciler
and no per-block phase branch — those were range-move only (§2.5) and are not built.

### 2.7 Fields

Built `Virtual Machine Migration` fields for the keep-address / transfer state
(`virtual_machine_migration.json`): `keep_address` (Check, set-once — the branch
switch), `forward_address` (Check — records a proxy-NDP-primary source, metadata
only, §2.8), `tunnel_status` (Select: Armed/Forwarding/TornDown), `tunnel_device`
(the `mig6-<vm8>` iface name — teardown / lost-task re-entry handle), `forward_active`
(Check — mirrors `tunnel_status == Forwarding`, gates the Collapse-forward button),
plus `ipv6_address_old`/`ipv6_address_new`, `identity_injected`, `nbd_port`/`nbd_pid`,
`root_disk_bytes`/`data_disk_bytes`, `base_ship_state`/`base_ship_percent`, and the
`hydration_*` fields.

> **Superseded fields (range-move), never built:** the row's `block_fip_id` and a
> `Server.pending_fip_move` / `fip_move_target` / `fip_move_fip_id` triple. The built
> tunnel field is `tunnel_device`, not `block_tunnel_device`.

### 2.8 Address-scheme detection (one capability)

Keep-address vs change-address is decided by **one Provider capability method**,
`vm_range_is_forwardable(provider_resource_id)` (`providers/base.py`, default `False`
→ Self-Managed and Fake fall back to change-address; Scaleway and DigitalOcean both
override `True`). It models a fact about the delivery mechanism — the source host
keeps holding the `/64` and can forward the `/128` — so it needs no provider API call.

At migration insert (`_decide_address_scheme`,
`migration_preflight._will_keep_address`):

```
keep_address    = vm_range_is_forwardable(source) and vm_range_is_forwardable(target)
forward_address = source is proxy-NDP-primary (DigitalOcean)   # metadata only
```

`forward_address` **no longer gates** anything — proxy-NDP re-assert is unconditional
for every keep-address provider (§2.0 fact 1). A future Self-Managed BGP path or any
new forwardable provider joins keep-address by flipping its own
`vm_range_is_forwardable`, with no migration-code change.

> **Superseded:** the earlier two-bit design (`vm_range_is_portable` for range-move
> **and** `vm_range_is_forwardable`) — `vm_range_is_portable` was never built.

### 2.9 Permanent per-VM forwarding (the built keep-address path)

The `/128` is kept fixed by having the **source host keep answering for it and
forward the matched traffic** to wherever the VM now lives — one tunnel per migrated
VM, brought up once and left up. No range moves; only the destination of one
`/128`'s traffic does. This is the keep-address path for **every forwardable
provider** (DigitalOcean and Scaleway today).

**Scope: today's operating context.** The source is assumed **never decommissioned**
(DigitalOcean is a fast dev platform here; Scaleway keeps its `/64`), so there is no
requirement to ever reclaim a forward. A drain/release story is explicitly **not**
built (see the *Open follow-up* at the end).

#### 2.9.0 What's reused from §2.1

The tunnel carrier (socat-bridged `tun`, no new public port), the source-forward
route/nft (§2.2), and the target return-route (§2.3) are all reused verbatim; only
the *keying* is per-VM, not per-block. Everything about *why* that carrier shape was
chosen (§2.1) carries over.

#### 2.9.1 The tunnel — one `tun` device per migrated VM

`mig6-<vm8>`, brought up on both ends in `TargetPreparing`
(`_bring_up_forward_tunnel` → `migration-forward-up`, idempotent), with
device/port/table all pure functions of the UUID (§2.1). MTU pinned to 1280;
host-probe throughput before production (§2.1 caveat).

#### 2.9.2 Source side — re-assert proxy-NDP, replace the delivery route

At cutover the source unit's `ExecStopPost` (`vm-network-down.py`) deletes this VM's
proxy-NDP entry along with the rest of its networking. `CutoverStarting`
**re-asserts the proxy-NDP entry** and points delivery at the tunnel
(`migration-source-forward`, §2.2): atomic `ip -6 route replace … dev mig6-<vm8>`,
the two nft forward rules, and `ip -6 neigh replace proxy <vmv6> dev <uplink>`. Unlike
routed delivery, the proxy-NDP entry is state this phase re-creates.

#### 2.9.3 Target side — normal `vm-network-up`, plus a return route

> **Open, not host-verified: does the provider's edge drop egress sourced from an
> address outside the droplet's own `/64`?** Until verified this takes the
> conservative branch §2.0 fact 2 takes for Scaleway: **assume dropped**, and route
> the return path back through the source (§2.3, symmetric). If a probe later shows
> the target *can* egress the source's range directly, the return route becomes
> unnecessary — a strictly easier case.

The target runs its normal `vm-network-up.py`; the one addition
(`migration-target-receive`, run **before** source-forward) is the return-route
policy (§2.3), keyed per-VM (`derive_vm_tunnel_table`). Inbound: source answers NDP →
tunnel → target veth → guest. Outbound: guest → veth → policy-routed to the tunnel →
source → source's own uplink.

#### 2.9.4 State-machine integration (keep-address phase deltas)

The per-VM phase **order is unchanged** (§3); the branch is `doc.keep_address`.

| Phase | change-address (Self-Managed fallback, §2.8) | keep-address (per-VM forward) |
|---|---|---|
| `Pending` | as §4 | as §4 (no block/drain checks) |
| `ExportingSnapshot` | as §5 | as §5 |
| `TargetPreparing` | build dm-clone | + **bring up the per-VM tunnel** (`migration-forward-up`, §2.9.1); status → Armed |
| `InjectingIdentity` | `allocate_ipv6(target)` → new `/128`, rewrite `network.env` | **near-no-op for networking:** `/128` unchanged (re-checked free on target), no `allocate`, no env rewrite; still injects non-address bits |
| `Hydrating` / `CollapseClone` | as §5 | as §5 |
| `CutoverStarting` | source down, target boots on clone | same, **then** target return-route then source-forward + proxy-NDP re-assert (§2.9.2–3); status → Forwarding |
| `Repointing` | fire `vm.address_changed` (Subdomain + proxy) | **no re-point** — flip `server` (`flags.migrating`), `status = Running`; `ipv6_address` copied verbatim |
| `Cleanup` | source teardown as §5 | same teardown **but** the tunnel + source-forward route/nft + proxy-NDP + target return-rule are **left up permanently** (`KEEP_ADDRESS=1` suppresses the `vm-network-down` re-run — §5 *Cleanup*); record the forward on the VM |

There is **no deferred reconciler** — the forward is part of the migrated VM's
permanent shape from `Cleanup` on, the same way its UUID-derived MAC/TAP/netns are.

Two cross-cutting cutover invariants owned by other chapters also fire here:

- **Private-plane withdraw-then-advertise ([31 §16.3](./31-ancp-network-control-plane.md)).** In
  `CutoverStarting`, the source's advertisement of the VM's **host-independent**
  private `/128` is withdrawn (`_withdraw_private_from_source`) **before** the
  target's `provision-vm` boots the guest and advertises the same `/128` — two hosts
  advertising it at once is the §7.3 conflict that makes ANCP drop it from every
  host's wg-mesh AllowedIPs and blackhole the private plane for the whole hydration
  window. Safe because the source VM has been Stopped since `Pending` (it stopped
  *serving* the `/128` long ago; this only stops it *advertising*).
- **Boot-epoch fence bump ([33 §11.1](./33-boat.md)).** `Repointing`'s
  `_finalize_cutover` bumps `boot_epoch` **once and only here**; past that line the
  losing source's Boat holds the old epoch and refuses a stale or partitioned start of
  the same UUID (`fence.Allow` → `ErrStaleEpoch`). The idempotency guard makes a
  re-entered `Repointing` bump the epoch a single time.

#### 2.9.5 Operator visibility and manual teardown

The forward is permanent by default, so it is surfaced and reversible:
`Virtual Machine Migration.forward_active` (set in `Cleanup`), a **dashboard
indicator** on the migrated VM ("Traffic forwarded from `<source>` since `<date>`"),
and a manual **"Collapse forward"** action (`migration_forward.collapse_forward`,
enabled only while `forward_active`). Collapse tears down the source's proxy-NDP entry
/ route / nft (deasserting NDP for every provider), the target's return-rule, and the
tunnel on both ends, then falls back to **change-address**: `allocate_ipv6(target)` a
new `/128`, **stop the VM** first (so `provision-vm`'s `systemctl start` actually
reboots onto the new address, and so the collapsed-linear clone releases the plain LV
it holds busy), re-provision in place preserving host keys, then fire
`vm.address_changed`. This is the **only** point at which a kept address can still
change, and it is entirely operator-initiated — never automatic.

> **Open follow-up, not built.** A source-decommission story (many VMs forwarding off
> one source the operator wants to retire) would be a per-VM sweep of Collapse-forward
> — one change-address migration per forwarded VM, never a single free `/64` re-point
> (there is no such API). Deliberately not designed further; the operating context
> doesn't call for it yet.

## 3. States

The `Virtual Machine` itself stays `Stopped` throughout (it flips to `Running` at
cutover). The phase machine lives on the migration row (`PHASE_ORDER`,
`migration.py`):

```
Pending
  │ (pre-flight; source unit's autostart disabled FIRST, VM stopped, mem-snapshot cleared)
  ▼
ExportingSnapshot ── source: thin-snap both LVs, start NBD
  ▼
TargetPreparing ──── target: pre-flight image+pool+modules, create thin LVs,
  │                          connect nbd client, dmsetup create …clone;
  │                          keep-address: also bring up the per-VM tunnel (§2.9.1)
  ▼
InjectingIdentity ── target: inject identity THROUGH the clone device;
  │                          change-address: new v6/v4 env; keep-address: near-no-op;
  │                          both: keep host keys
  ▼
CutoverStarting ──── source unit down; target boots on the dm-clone read-through;
  │                  DOWNTIME ENDS. keep-address: target return-route then
  │                  source-forward onto the tunnel (§2.9.2–3). Private /128
  │                  withdrawn from source FIRST (31 §16.3).
  ▼
Hydrating ────────── target: enable_hydration once; scheduler re-probes % each tick
  │                  while the guest SERVES; advance at 100%, Fail on stall
  ▼
CollapseClone ────── reload each 100%-hydrated dm-clone table clone→linear (§5),
  │                  transparently, guest live; disconnect the nbd client
  ▼
Repointing ───────── controller: commit row (server [+ ipv6 on change-address],
  │                  status + desired_power Running; boot_epoch bumped, 33 §11.1);
  │                  change-address: fire vm.address_changed; Reserved-IP (§6)
  ▼
Cleanup ──────────── source: kill NBD, lvremove migrate-snapshots, tear down the
  │                  stale source copy; keep-address: leave the tunnel + forward
  │                  route + proxy-NDP up for good (§2.9.4)
  ▼
Done                 terminal

(any phase) ──► Failed   error_message set; Retry re-enters the last non-Done phase
                         (all idempotent); Rollback before CutoverStarting just
                         restarts the intact source VM
```

**Why this order is safe.** The source VM is never destroyed until `Cleanup`, *after*
the target is confirmed `Running` and routing is re-pointed. Any failure through
`CutoverStarting` rolls back by starting the source VM again — its disk and `/128` are
untouched (on the keep-address path the `/128` never moved at all). `Subdomain` rows
are rewritten only in `Repointing` (change-address only), the point of no return.

**Stopped means stopped across a reboot: `Pending` disables the source unit's
autostart FIRST** (`_disable_source_autostart` → Boat `migration-source-autostart`,
`enabled=0`), *before* it stops the VM. `provision-vm` enabled
`firecracker-vm@<uuid>.service` with `[Install] WantedBy=multi-user.target`, whose
only condition is the *sleeping* marker — there is no migration condition. Without
this the source host's next reboot cold-boots a **second live copy** of the guest
anywhere between `Pending` and `Cleanup`: same UUID, same UUID-derived MAC/tap, same
host keys, and on a keep-address migration the same public `/128` answered by two
hosts — started by systemd's `multi-user.target.wants` symlink, with nothing asked. It
is a plain `disable` (never `--now`, never `mask`): the WantedBy symlink goes and
nothing else does, so the rollback above — an explicit `systemctl start` of the intact
source VM — still works. (A `ConditionPathExists=!` marker would block that explicit
start too.) A rollback that **abandons** the migration should re-enable the unit so
the resurrected source survives its host's next reboot.

**The power intent follows the guest.** `Pending`'s stop states
`desired_power = Stopped` on the VM row ([33 §11.3](./33-boat.md)); the cutover boots
the guest on the target, so `_finalize_cutover` walks that back to `Running` in the
same write as `status`. Leaving it Stopped is permanent drift the dangerous way round
— Stopped is the half that outranks everything, so `assert_desired_state()` would push
it at a live VM, `resize()` would re-state it, the VM could never sleep again, and a
host-initiated wake would never be adopted.

## 4. Pre-flight (the `Pending` gate)

`migrate()` and `preflight_checks` (`migration_preflight.py`) refuse to proceed
unless:

- target `Server.status == "Active"` **and visible to placement** (`assert_visible`
  — an arrival goes through the placement gate; a live VM must not be moved onto a
  host Atlas has lost sight of, since it is already stopped by then; the source is
  deliberately not gated: moving a VM *off* an unseen host is the migration an
  operator most wants);
- target and source are the **same provider** (`provider_type`; cross-provider is out
  of scope); region is same by construction (§1);
- the VM is not `Sleeping` (its RAM lives in a non-transportable on-host memory
  snapshot — wake or stop first) and is in `Stopped`/`Running`/`Paused` (a running VM
  is stopped first, with a **plain fast** stop — `graceful=False`,
  `MIGRATION_STOP_TIMEOUT_SECONDS`; a captured RAM image is worthless on the target,
  so `has_memory_snapshot` is forced to 0 and the `snapshot/` dir dropped);
- the base image is present on the **target** (checked on-host, the same probe
  `provision-vm` does). A **syncable** image that is absent fails loud and early; a
  **local** image (`is_local`, snapshot-promoted, no rootfs URL) is **shipped to the
  target during `TargetPreparing`** over NBD (§5.1) — it does *not* fail pre-flight;
- **change-address:** the target has IPv6 capacity (`allocate_ipv6` would succeed);
  **thin-pool headroom** is checked on every path regardless;
- **keep-address:** the kept `/128` is **not already live on a different VM on the
  target** (`_assert_kept_address_free`) — two VMs can't share a `/128` on one host
  (a single `<vmv6>/128 dev <veth>` route points at only one; the other silently
  steals the traffic — observed in the field);
- the VM's attached Reserved IP is handled per §6 (built: released with an explicit
  `release_reserved_ip=True` ack).

## 5. Storage: NBD export + dm-clone hydration

**Whole storage path VERIFIED end-to-end on real hosts (2026-07-02).** On two
Scaleway Elastic Metal hosts: NBD export → dm-clone hydrate → collapse, run against a
**real 4 GiB ext4 VM disk**, gives a **byte-identical** destination thin LV that
**mounts cleanly** as the full Ubuntu rootfs after collapse, ext4 UUID/LABEL
preserved; also verified cross-host at ~490 MiB/s. Three impl requirements surfaced
and are folded in below: (a) `qemu-nbd --persistent`, (b) parse `dmsetup status`
positional field 7, (c) the dest LV cannot be mounted until the clone is collapsed.

**Hydration acceptance.** The target **boots at any hydration %** (reading through to
the source over NBD — §0), but the source thin snapshot and NBD export are **held
alive until hydration hits 100%**, and `Cleanup` runs **only after** the dm-clone
collapses. This gives fast availability *and* a clean rollback window: the source VM
and its disk stay intact and re-startable through the entire `CutoverStarting` phase.

### Source side (`migration-export-source`, phase `ExportingSnapshot`)

Pre-flight pool headroom, take a thin CoW snapshot of the **Stopped** VM's root LV
(`atlas-snap-<uuid>-migrate`) and, if present, its data LV (a Stopped VM's filesystems
are cleanly unmounted, so the snapshot is flush-clean and, with two disks, mutually
consistent), then start `qemu-nbd` on a **per-VM UUID-derived port** (`nbd_port`,
`migration_layout.py`; avoids collisions under concurrent migrations), exporting the
snapshot(s) read-only. A data disk gets a **second** `qemu-nbd` on `port+1`. **Pass
`--persistent` (verified-required):** the live guest reads through NBD for the whole
hydration window, so a server that exits on first disconnect would fault the guest,
not just a background copy (`--shared=N` alone does not keep it alive past
disconnect). The controller records `nbd_port` / `nbd_pid` and each disk's actual byte
size. Driven from `_phase_exporting_snapshot`.

### Target side (`migration-clone-target`, phases `TargetPreparing` + `InjectingIdentity`)

Pre-flight (`dm_clone` + `nbd` modules, `qemu-nbd`/`nbd-client`, base image LV, pool
headroom — all ship at bootstrap, so this is a defensive re-assert), then connect
`nbd-client` to the source (**plain TCP to the source's public IPv4** in the built
stage — an SSH-tunnelled carrier is deferred, §2.1), create the fresh thin LV
`atlas-vm-<uuid>` sized at **the max of the VM's `disk_gigabytes` and the source's
actual bytes** (`_target_disk_gb`: a grown or CoW-of-larger-base disk is physically
bigger than the doc says, and under-sizing truncates the filesystem → dead superblock
at cutover — **never under-size; growing to match is safe**), plus the data LV and a
small clone-meta LV, and build the dm-clone. Each VM's target nbd devices are a
UUID-derived 4-slot block (`nbd_base_slot`: root/data/base/tar) so concurrent
migrations to one target don't collide.

**Identity inject before any boot** (`InjectingIdentity`, `_phase_injecting_identity`):
mount the **clone mapper device** `/dev/mapper/atlas-vm-<uuid>-clone` — **not** the
bare `atlas-vm-<uuid>` thin LV, which is held open by the clone and mounts `busy`
(verified 2026-07-02). Writes through the clone land on the dest and count toward
hydration; the plain LV is only mountable after collapse. **Change-address** rewrites
`/etc/atlas-network.env` with the new `/128`, NAT44 `/30`, v4 gateway, and data-disk
fstab; **keep-address** leaves the network env untouched (the `/128` is unchanged).
Both preserve **host keys** (`regenerate_host_keys=False`, exactly as
`rebuild`/`restore` do).

### 5.1 Local base image ship (`migration-export-base` + `migration-receive-base`)

A VM whose base image is **local** (`is_local` — snapshot-promoted, no rootfs URL;
spec/08-images.md) cannot be synced: the `atlas-image-<image>` base LV lives only on
the source. It is shipped to the target the **same way the disk is** — an NBD export
the target flattens into a fresh local LV — as the **first step of `TargetPreparing`**
(`_ensure_base_on_target`), a no-op for a syncable/present image. Two artifacts ride
the disk export's spare ports: the read-only base LV as a block export (port
`nbd_port+2`, exported directly since the base is immutable) and a **file-backed** NBD
export of a tar of the image dir (`kernel` + sentinel, port `nbd_port+3` — how the
kernel reaches the target with no host-to-host copy). The target hydrates a local base
LV via dm-clone + extracts the tar, polls hydration to 100%
(`migration-poll-hydration --clone-device atlas-base-<image>-clone`, per-tick percent
on `base_ship_percent`), then collapses and marks the LV read-only. `TargetPreparing`
is **non-advancing while a base ships** (re-enters, like `Hydrating`); Cleanup kills
the `+2`/`+3` exports; the source's base LV is never removed. A dropped nbd link
mid-ship self-heals (rebuild the wedged base clone), exactly like the disk path.

### Data disk

A second dm-clone over a second NBD export (root = `nbd_port`, data = `nbd_port+1`),
symmetric with the root disk (same idempotency + hydration-poll machinery), and
**available immediately** too. Both disks must be 100% before collapse. The
blocking-`dd` alternative was rejected — it would leave the data disk unusable until
the copy finished.

### Hydration (`migration-poll-hydration`, phase `Hydrating`)

`dmsetup message <dev> 0 enable_hydration` **once** per disk, then the **scheduler**
re-enters each tick with a short read-only status probe, recording `hydration_percent`
(the min across both disks) and advancing only at 100%. **Parsing (verified):**
`dmsetup status` emits **no `hydration` label** — read the `<hydrated>/<total>` pair
from **positional field 7** (1-indexed), not a keyword grep; dm-clone pre-marks a few
zero/discard regions hydrated at create time but the dest is still byte-identical at
100%. This keeps a multi-minute copy **off the worker** — a sequence of cheap polls,
not a held job. Stall guard: no progress for `HYDRATION_STALL_TICKS` → `Failed`. A
dead nbd client (reads return 0, hydration frozen) is **self-healed**, not counted as
a stall: the clone pins the dead device open, so it is torn down and rebuilt (re-run
the prepare step, which detects the wedged stack, re-dials, recreates the clone) and
hydration resumes from 0.

### Collapse (`migration-cutover-target`, phase `CollapseClone`)

After hydration is 100%, each dm-clone is collapsed **transparently while the guest
is live** (`_phase_collapse_clone`): the script **suspends the clone, reloads its
table from `clone` to a `linear` map onto the plain dest LV, and resumes** — the dm
device keeps the **same major:minor**, so Firecracker's open rootfs fd survives.
`dmsetup remove` on that open fd fails "Device or resource busy" (host-verified on
real f1 thin LVs, 2026-07-02); reload-to-linear does not. The source nbd client is
then disconnected. Idempotent: a no-op on a clone already carrying a linear table or
already gone.

### Cleanup (`migration-cleanup-source`, phase `Cleanup`)

Kill the NBD server(s) by recorded pid (no-op if gone), `lvremove` both `-migrate`
snapshots (guarded against base images), and tear down the **stale source copy** —
old per-VM directory, LVs, netns, veth, proxy-NDP — with the same teardown
`terminate-vm.py` performs, against the *old* host. That teardown re-runs
`vm-network-down.py`, which on a change-address migration is a harmless defensive
sweep (the source unit's `ExecStopPost` already ran it at `Pending`). **On a
keep-address migration it is NOT harmless and must not run:** `vm-network-down.py`
removes the proxy-NDP entry for the `/128` and sweeps **every** `inet atlas forward`
rule mentioning it — exactly the two rules `migration-source-forward` installed — so
running it after a kept-address cutover **black-holes the tenant's public ingress**
(egress still works) while Atlas reports `Done`. `Cleanup` passes `KEEP_ADDRESS=1` so
that one step is skipped; everything else — NBD, snapshots, unit, directory, LVs — is
unchanged, and the tunnel + forward route + nft + proxy-NDP + target return-rule are
left up permanently (§2.9.4). The forward is then recorded on the VM
(`_record_forward_on_vm`, §2.9.5).

If any step fails the row stays at `Cleanup` with manual-recovery guidance in
`error_message`: there is **no orphaned-LV reconciler**, so the row's visibility *is*
the backstop (consistent with [18](./18-bench-self-routing.md)'s "no sweeper" stance).

## 6. Reserved IP (public IPv4)

> **Status: the reassign-preserve design below is NOT built.** Built stage-1
> behaviour (`_handle_reserved_ip`): an attached Reserved IP is **detached and
> dropped** on the source, and pre-flight **requires** an explicit
> `release_reserved_ip=True` ack (else it throws) so it is not a surprise; the
> operator re-attaches a target-server Reserved IP afterward. The customer's inbound
> v4 is **not** preserved across the move yet.

*Planned (not built):* preserve the inbound v4 by reassigning the vendor Reserved IP
to the target and repointing the row. This relaxes `Reserved IP.server` immutability
([02 § Reserved IP](./02-doctypes.md#reserved-ip)) — the IP stays bound to its
address + vendor handle for life, but which Server it points at becomes a mutable
pointer (and an IP may rest with no Server) — and adds a `reassign(target_server)`
method, so `Repointing` would `detach()` on the source, `reassign(target_server)` at
the vendor, and `attach()` on the target, leaving the IP and any DNS A record
unchanged. Until then the default is **drop**, gated on the ack. (Self-Managed has no
vendor bind, so `reassign` would only repoint the row.)

## 7. The callback: resumability across API issues and rate limits

A migration self-drives: `start_migration` (`resumable.self_drive`, enqueued by
`VirtualMachine.migrate` on insert) runs one phase — or one `Hydrating` poll — then
re-enqueues itself, walking `Pending → … → Done` on its own, self-pacing the long
copy on the inline poll's round-trip with no wait for a cron tick.
`reconcile_migrations()` (a `scheduler_events` `cron` entry, ~every 2 minutes,
`hooks.py`) is the **safety net**: it re-drives any non-terminal migration whose
self-drive job was dropped (worker crash, OOM), inside a **try/except per row** so one
stuck migration never blocks the others and a failure marks just that row `Failed`.
`advance_migration` reads `status`, checks the phase's idempotency key ("am I already
done?"), and runs the phase **inline via `run_task` / `run_boat_migration_phase`**
(not `frappe.enqueue` — inline avoids the lost-worker-job class and saves the Task row
first, raising on failure). A phase Task still `Running`/`Pending` past `2×` its
timeout is treated as **lost** and the phase re-entered idempotently — recorded, never
a silent duplicate (`_detect_lost_task`).

## 8. Operator UX

One **Migrate** button on the `Virtual Machine` form creates the
`Virtual Machine Migration` row (target-server picker + the optional
`release_reserved_ip` ack); the self-drive + scheduler drive it. The Migration form
shows the phase pill, hydration %, `tunnel_status`, and a **Retry** on `Failed`.
Per-phase manual buttons are an optional debug affordance only, not the primary flow;
the lifecycle guard (`_guard_no_active_migration`) blocks concurrent lifecycle actions
on a VM mid-migration regardless.

## 9. Build order (staged rollout)

Built in stages, each a mergeable increment, because change-address and keep-address
are parallel branches of the *same* phase table (§2.9.4), not two designs:
**(1) change-address only** (the hard part — NBD export, dm-clone hydration, identity
injection, cutover, and UUID/host-keys/disk/Subdomain-links surviving onto a new
`server`); **(2) proxy repoint** (`Repointing`'s `vm.address_changed`, so a site or
bench migrates cleanly end-to-end); **(3) keep-address** (the per-VM tunnel §2.1–2.3,
§2.9, wiring in the real `vm_range_is_forwardable` capability so `keep_address` is
computed, not forced); **(4) drop the repoint on the keep path** (the Subdomain
rewrite is dead weight when the address never changed — §2.9.4 skips it). The
change-address code is **not** deleted: it remains the Self-Managed fallback (§2.8)
and the Collapse-forward escape hatch (§2.9.5), both of which still need it. The
Scaleway `/64` **range-move** that an earlier plan slated for stage 3 was superseded
by the per-VM forward and never built (§2.1–§2.7).

## New dependencies

The cold-migration disk move needs `qemu-nbd`/`nbd-client` (userspace) and the `nbd`
+ `dm_clone` **kernel modules**, all folded into `bootstrap-server.py` so every
Active host can be a source or target without re-bootstrap: `qemu-utils` +
`nbd-client` + `socat` (the §2.1 tunnel carrier) join the apt set (verified to
install cleanly on a live Ubuntu 24.04 host, 2026-07-02), and a dedicated step
installs `linux-modules-extra-$(uname -r)` (version-pinned, never the floating
`-generic`) and persists `nbd` + `dm_clone` via
`/etc/modules-load.d/60-atlas-migration.conf`. `CONFIG_DM_CLONE` merged in kernel 6.4;
Ubuntu 24.04 ships 6.8. [README § principle 5](./README.md) and
[03-bootstrapping.md](./03-bootstrapping.md) match. The target clone script still
defensively re-asserts the modules with a clear "re-bootstrap" message for hosts
predating this.

## Implementation

The built code is the source of truth:

- `atlas/atlas/core/migration.py` — the resumable phase machine
  (`reconcile_migrations`, `start_migration`, `advance_migration`, every phase),
  `_finalize_cutover` (the `server`/`ipv6`/`boot_epoch` commit), and the private-plane
  cutover seams (`_withdraw_private_from_source`, `_repoint_private_plane`).
- `atlas/atlas/core/migration_preflight.py` — the pre-flight gate and the
  `_will_keep_address` address-scheme decision.
- `atlas/atlas/core/migration_forward.py` — the operator-initiated `collapse_forward`
  (§2.9.5).
- `atlas/atlas/core/migration_layout.py` — the UUID-derived host layout
  (`clone_device_path`, `nbd_port`, `nbd_base_slot`).
- `providers/{base,scaleway,digitalocean}.py::vm_range_is_forwardable` — the
  keep-address capability (§2.8).
- The host-side `migration-*` work runs as **Boat migrate phases** (spec/33), not
  in-repo Task scripts.

> An earlier illustrative draft (change-address only, detach-and-drop Reserved IP)
> predating the §2 keep-address and §6 decisions lives in
> [`spec/samples/migration/`](./samples/migration/). **Build from the code and this
> spec, not the sample, where they differ.**

## Testing

- **Unit** (`test_virtual_machine_migration.py`, `test_boat_migration_phase.py`): the
  `flags.migrating` immutability exception; the per-VM single-migration and lifecycle
  guards; each phase's idempotency key; lost-Task re-entry; the change-address re-point
  (`vm.address_changed`) vs the keep-address branch (no re-point, address copied); the
  `vm_range_is_forwardable` gate and the `keep_address`/`forward_address` derivation
  from it; the kept-`/128`-free pre-flight throw; `Cleanup` passing `KEEP_ADDRESS` so
  the forward survives its own teardown; `Pending` disabling the source unit's
  autostart **first**; and `desired_power`/`boot_epoch` coming to `Running`/bumped at
  cutover (and at Collapse-forward). The two source-side steps are Boat migrate
  phases; their host behaviour (a kept address's forward is not torn down; the
  autostart toggle is a plain `disable`) is proven by Boat's own tests, and the
  Atlas-side wire mapping — `cleanup-source`'s `keep_address` and `source-autostart`'s
  `enabled` bool — by `test_boat_migration_phase.py`.
- **E2E** (`atlas/tests/e2e/use_cases/virtual_machine_migration.py`): real servers,
  full phase progression, one scenario per address scheme:
  - **Change-address (Self-Managed fallback):** the new `/128` is in the target's
    range, `server` flipped, Subdomains re-pointed and the proxy synced.
  - **Keep-address (per-VM forward, Scaleway/DigitalOcean):** `ipv6_address`
    **unchanged**; reach the VM's `/128` from off-host through the source's proxy-NDP
    + tunnel (poll well past any drain window, to prove there is no auto-teardown);
    `forward_active`/`tunnel_status` stay `1`/`Forwarding` with no scheduler action
    collapsing them; then explicitly invoke Collapse-forward and assert it falls
    through to change-address (new `/128`, Subdomains re-pointed, tunnel + proxy-NDP
    gone).
  - **All:** source LVs/dir gone, SSH host keys **unchanged** across the move, and the
    Reserved-IP ack path honoured.

This becomes a new row in [README § Operator use cases](./README.md) once surfaced:
`Virtual Machine → Migrate`.
