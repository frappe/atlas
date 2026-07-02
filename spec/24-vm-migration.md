# Virtual-machine migration between hosts

> **Status: DESIGN (not built).** This document is a deliberate spec-first
> capture of the migration design, drafted from a deep read of the existing
> code and an adversarial design pass. Nothing here is implemented yet; the
> sample implementation lives beside it (see *Sample implementation* at the
> end) as illustrative, not committed, code. It is built in four stages — see
> [§9 Build order](#9-build-order-staged-rollout) — starting with
> change-address-only migration and ending with the keep-address networking
> below made the only path. When the final stage lands, drop this banner and
> fold the lifecycle changes into [05](./05-virtual-machine-lifecycle.md),
> the addressing changes into [06](./06-networking.md), and the new host
> dependencies into [README § Operating principles](./README.md) (already
> done for the bootstrap dep set — see *New dependencies* below).

Migration moves a **Stopped** virtual machine's disk(s) from a **source**
server to a **target** server, keeping the VM's identity (its UUID and
everything derived from it), with minimal downtime. It is **cold** migration:
the guest is shut down during cutover. The guest's **RAM never crosses hosts** —
that is a [non-goal](./README.md) and a hard Firecracker constraint (a
memory-state snapshot only restores on a matching CPU model / host kernel /
Firecracker build; see [05 § Warm snapshot fan-out](./05-virtual-machine-lifecycle.md#warm-snapshot-fan-out-one-golden-n-restored-clones)).
Only the **disk** moves.

The disk moves over **NBD** (the source exports a crash-consistent LVM thin
snapshot of the VM's disk LV, over an **SSH tunnel** — the only path between
two hosts, since [06](./06-networking.md) gives hosts no private fabric) into a
device-mapper **`clone`** target on the destination: the target VM **boots
immediately**, reading-through to the source over NBD while a background
*hydration* copies every block locally; once hydration is 100% the dm-clone
collapses to the plain thin LV and the NBD export is torn down.

The whole operation is a **resumable phase state machine** recorded on a new
`Virtual Machine Migration` row and advanced by a scheduled
`reconcile_migrations` job — the **callback** that makes it survive a provider
rate-limit, a dropped RQ job, or an SSH blip. Each phase is idempotent and is
re-entered from the row's recorded `status`.

## Why this shape

Three decisions dominate.

### 1. The UUID and the IPv6 `/128` are both preserved; only `server` changes

A `Virtual Machine.name` is a UUID, immutable forever, and **everything
host-local is derived from it** — MAC (`derive_mac`), TAP (`derive_tap`),
network namespace (`derive_netns`), per-VM uid (`derive_uid`), veth pair
(`derive_veth_pair`) (see [06](./06-networking.md), `atlas/atlas/networking.py`).
These are pure functions of the UUID, so the target host re-derives them
**identically** — no collision risk (the names are unique per UUID, and the
source's are torn down). The VM keeps its SSH host keys, its history, and its
links.

`server` is in `IMMUTABLE_AFTER_INSERT` and `validate()` throws on any change
([05 § Why resource fields are frozen](./05-virtual-machine-lifecycle.md#why-resource-fields-are-frozen-outside-resize),
`virtual_machine.py`). Migration is the **one sanctioned path** that repoints
it, gated by a `flags.migrating` branch in `validate()` that mirrors the proven
`flags.resizing` pattern (the resize path's exact mechanism for letting an
otherwise-frozen field through). The alternative — create a new VM on the
target and terminate the old — was rejected: it burns the UUID, breaks the SSH
identity, and orphans every `Subdomain` (whose `virtual_machine` field is
**itself** immutable, so the routes can't follow a new VM).

On the **keep-address** path (Scaleway — §2; DigitalOcean — §2.9) the VM also
keeps its `ipv6_address`, so `server` is the **only** field that changes. On the
**change-address** path (Self-Managed without operator-wired BGP — §2.8)
`ipv6_address` changes too, the same `flags.migrating` gate lets it through.

### 2. The public IPv6 `/128` is preserved and routed across hosts

This **reverses the earlier sketch** ("the /128 always changes; re-point the
proxy"). Two independent mechanisms both land on keep-address, for different
reasons:

- On a provider whose VM range is a **portable routed prefix** — a Scaleway
  flexible `/64`, the resolved answer to the old open question Q3 — the VM keeps
  its `/128` because the **range itself** moves: the source host routes the
  `/128` to the target over a host-to-host tunnel for the transition window,
  and once every VM sharing the source's `/64` has migrated or terminated, the
  `/64` flexible IP moves to the target with one provider API call. This is
  **§2.1–§2.7** below.
- On DigitalOcean, the carved `/124` is **not** portable — no API moves it —
  but we keep the `/128` anyway by **forwarding it permanently**: the source
  host keeps answering for the address (proxy-NDP) and tunnels it to the target
  forever, since the range never moves and nothing ever reclaims it. This is
  **§2.9**.

Because the `/128` is preserved on both paths, `derive_ipv4_link(ipv6)` is
unchanged, so the NAT44 `/30`, `VIRTUAL_MACHINE_IPV4*`, and
`/etc/atlas-network.env` are byte-identical — **no network-env re-injection** —
and every `Subdomain.address` (the denormalized VM `/128`) is already correct,
so the **proxy/Subdomain re-point and `reconcile_region` are eliminated** on
both. They survive only on the change-address fallback (§2.8, §3).

### 3. The proxy/Subdomain re-point (change-address path only) is an explicit write **and** reconcile

`Subdomain.address` (the denormalized `/128` the proxy dials) is `read_only`,
refreshed from `Virtual Machine.ipv6_address` only inside `_denormalize_address`
on `validate()`; and `Subdomain.on_update` reconciles the fleet **only when
`active` flips** (`subdomain.py`). So neither a stale save nor a quiet field
write reaches the proxy. On the **change-address** path the migration therefore
does both halves explicitly in `Repointing`:

- `frappe.db.set_value("Subdomain", name, "address", target_ipv6)` for every
  Subdomain whose `virtual_machine` is this VM (the `address` write that the
  read-only/validate path won't do for us), then
- `reconcile_region(region)` (`atlas/atlas/proxy.py`) for each distinct region
  touched (the push the `on_update` hook won't fire on its own).

On the **keep-address** path neither write happens: the address is unchanged, so
the rows are already correct and the proxy already dials the right `/128` — the
moved `/64` re-routes that same `/128` to the target.

> **Region.** Each Atlas instance operates in **one region**, so a migration's
> source and target are always in the same region by construction — there is no
> cross-region case to guard, and `Subdomain.region` (immutable) and the
> Reserved-IP region binding are both satisfied trivially. The VM's `region`
> field is **copied from the source** verbatim (it is unchanged by the move).
> The region fields are slated for removal once the single-region invariant is
> made structural; until then migration neither reads nor compares them beyond
> the copy. (This resolves the earlier open question about a `Server → region`
> mapping: with one region per instance, no such mapping is needed.)

## 2. The IPv6 `/128` cross-host routing (keep-address paths)

This section is the heart of the keep-address design. §2.1–§2.7 are
**Scaleway-only** — they depend on the `/64` itself being a portable, routed
provider resource, which is a Scaleway-specific fact. DigitalOcean gets its own
keep-address mechanism, permanent per-VM forwarding rather than a range move,
in **§2.9**; Self-Managed falls through to the change-address path (§2.8)
unless an operator wires BGP re-announce.

Whether a given migration takes a keep-address path at all, and which one, is
decided by a pair of Provider **capability** methods, never a
`provider == "Scaleway"` / `"DigitalOcean"` literal (§2.8).

### 2.0 The two transit facts the design is built on

Two provider facts (verified against [06](./06-networking.md) and
`providers/scaleway.py`) dictate the whole shape, and correct the naive
"brief inbound-only forward" framing:

1. **Delivery is by the routed flexible IP, not by NDP.** A Scaleway flexible
   `/64` is *routed* to whichever server holds the FIP; on a routed prefix
   proxy-NDP is a documented **no-op** (the upstream router already knows the
   box — [06](./06-networking.md)). The two Elastic Metal boxes are **not** on a
   shared NDP segment toward the edge, so the target answering NDP for the `/128`
   is **not** a delivery race and changes nothing. We therefore build **no**
   "suppress NDP on target / re-assert on source" mechanism — `vm-network-up.py`
   and `vm-network-down.py` are **unchanged** (their unconditional proxy-NDP
   add/del is already a no-op on a routed prefix). The **route is the only
   load-bearing lever.**

2. **Egress is source-address-validated at the switch — VERIFIED (2026-07-02).**
   [06](./06-networking.md)'s routed-reserved-IP path does no SNAT and no egress
   policy route because "the vendor already accepts traffic sourced from the routed
   IP" — i.e. the FIP holder *may* source the prefix. The **negative** half — that a
   box sourcing an address it doesn't legitimately hold is dropped — was an unproven
   assumption; it has now been **host-verified on the two real Scaleway Elastic Metal
   hosts** and is in fact **stronger** than BCP38-by-prefix:
   - Probe (authoritative UDP receiver on the far host, 3 packets per source):
     packets sourced from the host's **SLAAC address arrive**; packets sourced from a
     freshly-added `/128` — **including one from the host's *own* on-link prefix** —
     are **dropped in the fabric** (they leave the host stack, the route resolves, the
     gateway neighbor is REACHABLE, but they never arrive). Generating NDP from the
     added source first does not help.
   - **Implication: host-side `ip addr add` is NOT a valid egress claim.** The switch
     validates against the SLAAC-assigned address, not the prefix. This **disproves**
     the earlier optimistic hypothesis ("the switch lets any host egress any IP by
     claiming it on an interface, no API") — that is false for host-side claim on
     Scaleway. The only thing that re-points egress is the **vendor API `attach()`**
     (which reprograms the switch) — but see the co-tenant constraint below.
   - Therefore the **conservative branch is the only correct one**: during the window
     the `/64` routes to the **source**, so only the source may egress the VM's
     `/128`. The tunnel is **bidirectional and full-bandwidth for the whole window** —
     inbound lands at the source and goes down the tunnel to the target; the guest's
     replies come back **up the tunnel** to the source and egress there. Asymmetric
     return (the target egressing directly) **does not work** — confirmed, not
     assumed. The "strictly easier inbound-only" branch **does not apply on Scaleway**.
   - **Vendor `attach()` is NOT an alternative to the tunnel for a per-VM move.** A
     flexible IP is a routed **/64**, and `attach()` grants egress by re-pointing the
     *whole prefix* to the target — which simultaneously **blocks ingress and egress
     for every co-tenant VM still sharing that `/64`** (no `/128` granularity at the
     switch). The steady state spreads a `/64`'s VMs across hosts, so a **partial**
     migration cannot use `attach()` without stranding the siblings. `attach()` is
     correct **only as the final step of a complete host drain** — once *every* VM on
     that `/64` has left the source, flip the FIP to the target and tear the tunnels
     down. For all ordinary (partial) migrations the tunnel is **mandatory**, which
     also puts the host-to-host SSH credential gap (§2.1) squarely on the critical
     path.

### 2.1 The tunnel — a TUN point-to-point link inside the existing SSH transport

Hosts have **no private fabric** ([06](./06-networking.md)); the only sanctioned
host-to-host path is the SSH tunnel the migration already uses for NBD
(`ssh -f -N -L 127.0.0.1:p:127.0.0.1:p root@source`, a **TCP stream** on the
durable Atlas key). We must not open a new unauthenticated public-internet path
(a bare `ip6ip6`/`ip6gre` proto-41/47 hole authenticated only by source address
would violate that). A raw L3 tunnel device speaks IP-proto-41/47, which a TCP
`LocalForward` cannot carry — so the carrier must be a device whose frames are an
ordinary **byte stream** SSH already forwards.

> **Genuinely open, host-verified 2026-07-02: "the durable Atlas key" this
> section leans on does not exist.** Every SSH path in this codebase today
> (`atlas/atlas/_ssh/transport.py`, `Connection.ssh_private_key`) is
> **controller → host**: the Frappe controller holds the one key and pushes its
> public half into each host's `authorized_keys` at bootstrap. §5's NBD tunnel
> and this section's TUN tunnel both need the **target host itself** to open
> `ssh ... root@source` as a subprocess of a Task script running *on* the
> target — which requires the target to hold a credential the source trusts.
> Verified directly against the two real hosts: neither has any private key or
> `IdentityFile` that lets it reach the other, and both hosts' `authorized_keys`
> contain only the single controller key. **This must be resolved before build
> order stage 3** (not stage 1–2, which need no tunnel). The candidates were (a)
> push the controller's own key to every host's `authorized_keys` so hosts
> already trust it mutually — no new distribution mechanism, but every host then
> accepts SSH from every other Active host, not just the controller, and the
> fleet-wide private key now has to live on every host (blast radius); (b) a
> dedicated inter-host keypair minted at bootstrap and fanned out to every other
> Active host's `authorized_keys`, needing its own join/leave reconciler; (c) the
> controller brokers the byte stream itself (proxies between two controller→host
> connections) instead of true host-to-host SSH, which avoids new credential
> distribution but funnels the whole migration data path through the controller;
> (d) each host mints its **own** keypair and exchanges only public keys
> (mirroring the pattern already in `atlas/atlas/wireguard.py`: "the host mints
> its own keypair… never routed through a Task and never stored in the Frappe
> DB") — no shared private secret ever crosses hosts or touches the controller's
> fleet-wide key.
>
> **DECISION (2026-07-02): option (d) — per-host self-minted keypairs, exchange
> public halves only.** Rationale: (a) is rejected because putting the
> controller's fleet-wide private key on every host is an unacceptable blast
> radius; (c) is rejected because it routes the full-window, full-bandwidth
> migration byte stream (§2.0 made this bidirectional and long-lived, not a brief
> blip) through the controller — a scaling and single-point-of-failure hazard the
> direct host-to-host TCP path (confirmed open below) specifically lets us avoid.
> Between (b) and (d), (d) wins because the codebase **already** has the
> self-mint-and-exchange-public-keys machinery in `wireguard.py`, so there is no
> new shared secret and no new private-key distribution mechanism — only public
> keys move. Mechanism: at bootstrap each host generates an ed25519 keypair under
> `/etc/atlas/host_id` (private stays on the host, 0600, never leaves, never in
> the DB); the host registers only its **public** key + reachable address with
> the controller. A small **authorized-key reconciler** (same join/leave shape as
> the WireGuard peer reconciler) writes every *other* Active host's public key
> into this host's `authorized_keys`, scoped with a `from=` restriction to Active
> host addresses and a forced-command/`restrict` so an inter-host key can open
> **only** the migration tunnel + NBD forward, nothing else. The target opens
> `ssh -i /etc/atlas/host_id root@source …` for both the NBD `LocalForward` (§5)
> and the `socat` TUN carrier (this section). "No new credential" (next
> paragraph) is now true once this reconciler ships — it is the stage-3
> prerequisite.
>
> **Connectivity itself is not the gap — confirmed separately, 2026-07-02:**
> raw unauthenticated TCP connects cleanly in both directions between the two
> real hosts' public IPv4s, no timeout, no refused, no vendor firewall in the
> way (`INPUT` policy `ACCEPT` on both, no blocking rule). "Hosts have no
> private fabric" above is about the absence of a *dedicated* network, not an
> inability to reach each other's public IPs — they can, freely, at the TCP
> layer. So whichever auth option above is picked needs no vendor
> firewall/routing change, only application-layer work. (UDP was tried too,
> for a possible WireGuard-based option (d): lossy in a quick single-packet
> test, not confirmed reliable — would need a proper sustained test before
> ruling that in or out.)

**Decision: a `tun` interface on each host, bridged to a dedicated SSH-forwarded
TCP stream by `socat`.** TUN (not TAP) because we carry exactly one L3 family
(the inner IPv6 `/128`); the v4 NAT44 `/30` is host-local egress only and never
crosses the move. No new firewall port, no proto-41/47 hole — the
[06](./06-networking.md) constraint is honored *and* packets actually flow (the
credential question above is resolved — option (d), per-host self-minted keys).

**Naming — keyed to the `/64` block, not the VM.** One tunnel carries **every**
`/128` of a draining `/64` (the `/64` is the migration unit, §2.4), so the device
name is a pure function of the source **flexible-IP UUID**: `mig6-<first 8 hex of
fip_id>` (≤15 chars, IFNAMSIZ-safe, same discipline as `derive_tap`). Both hosts
derive it identically. Two new pure helpers in `networking.py`:
`derive_block_tunnel(fip_id)` (the iface name) and `derive_block_tunnel_port(fip_id)`
(a per-block localhost port, mirroring `_nbd_port`'s derivation, in a
non-overlapping range).

A typed `migration-tunnel-up.py` Task brings it up on **both** ends in
`TargetPreparing` (idempotent / once per block). Source side: `socat
TCP4-LISTEN:<port>,bind=127.0.0.1,reuseaddr,fork TUN,tun-name=mig6-<fip8>,iff-up,iff-no-pi`,
then `ip -6 addr replace fe80::a/64 dev mig6-<fip8> nodad; ip link set … up`.
Target side: open the SSH `LocalForward` for `<port>` (idempotent, exactly like
NBD), then `socat TCP4:127.0.0.1:<port>,retry,forever TUN,tun-name=mig6-<fip8>,…`
+ `ip -6 addr replace fe80::b/64 dev … ; ip link set … up`.
`net.ipv6.conf.all.forwarding=1` (set at bootstrap, re-applied by
`vm-network-up.py`) lets each host forward between this TUN and the per-VM veth.

> **Genuinely open for the operator: TUN-over-`socat`-over-SSH throughput/MTU
> under live load.** The tunnel carries real bidirectional customer traffic for
> the whole drain window (§2.0 makes it full-duplex, not brief). Encapsulation
> overhead plus head-of-line blocking on a single TCP stream is untested at load.
> Pin the TUN MTU to the IPv6 minimum (`ip link set … mtu 1280`) to avoid
> in-tunnel PMTU surprises, and **host-probe throughput before draining a large
> block.** The carrier is swappable (the rest of §2 is carrier-agnostic): the
> fallback is a real `ip6tnl` between the two `Server.ipv6_address` host `/128`s,
> but that reintroduces the public-internet path [06](./06-networking.md) forbids,
> so it stays an operator-gated escape hatch, **not** the default.

### 2.2 Source-side forward — swap the veth route for the tunnel route

After cutover (target unit Running, source unit `disabled --now`), the source's
`vm-network-down.py` (`ExecStopPost`) has already torn down this VM's
netns/veth/tap and deleted its `<vmv6>/128 via fe80::3 dev <host_veth>` route.
But the source **still holds the FIP**, so inbound for the `/128` still lands
there. A typed `migration-source-forward.py` Task (`CutoverStarting`, **after**
the source unit is down) re-establishes reachability onto the tunnel:

```
ip -6 route replace <vmv6>/128 dev mig6-<fip8>
nft add rule inet atlas forward ip6 daddr <vmv6> oifname mig6-<fip8> accept
nft add rule inet atlas forward ip6 saddr <vmv6> iifname mig6-<fip8> accept
```

This is an **atomic** `ip -6 route replace` (single rtnetlink op) — no
delete-then-add black hole — and `vm-network-down.py` already removed the
competing same-length local route, so there is no specificity contest.
Proxy-NDP is irrelevant (no-op on the routed `/64`); we neither re-assert nor
suppress it. The return path is handled on the target (§2.3); the two `nft` rules
admit both directions.

### 2.3 Target-side receive — normal `vm-network-up`, plus a BCP38 return route

The target runs its **normal `vm-network-up.py`** at unit start (same
netns/veth/tap, the same `<vmv6>/128 via fe80::3 dev <host_veth>` route, the same
no-op proxy-NDP). **No change to `vm-network-up.py` is needed.**

The one target-specific addition (a `migration-target-receive.py` Task in
`CutoverStarting`, run **before** the source-forward so the return path exists
first) is the **return-route policy** that forces the guest's replies back up the
tunnel rather than out the target's own (spoof-dropped) uplink:

```
ip -6 rule add from <vmv6> lookup <block_table> priority 100
ip -6 route replace default dev mig6-<fip8> table <block_table>
```

where `<block_table>` is a small fixed table id derived from the FIP (shared by
every `/128` of the block; one `from <vmv6>` rule per migrated VM). This is the
load-bearing fix for the BCP38 egress drop (§2.0): inbound arrives via
source→tunnel→veth, and **outbound is policy-routed veth→tunnel→source→uplink**,
so every customer-facing packet is sourced from the box that owns the `/64`.
Inbound forwarding is already covered by the existing `vm-network-up.py`
`daddr <vmv6> oifname <host_veth> accept` rule; the existing `saddr <vmv6>
iifname <host_veth> accept` admits the reply leaving the veth toward the tunnel.

### 2.4 Block-drain rule and the multi-target edge case

**"The block"** on Scaleway is the whole flexible `/64` = the source's
`ipv6_virtual_machine_range`. Every non-Terminated VM on the source is carved
from it. The `/64` may move when, on the source,

```
count(Virtual Machine where server == source and status != "Terminated") == 0
```

VMs only ever leave the source (migrate → `server` flips to target, or
terminate), so the count is non-increasing and the predicate converges once every
VM is migrated or terminated.

**Edge case — a `/64`'s VMs fan out to two targets. Rule: drain-to-one-target
(pre-flight throw).** A keep-address migration is admissible only if it targets
the **same** target as every other in-flight/done keep-address migration draining
this source's `/64`. `preflight_checks` throws otherwise ("Source /64 is already
draining to <other-target>; a Scaleway flexible /64 can attach to exactly one
server, so all VMs sharing it must migrate to the same target."). A flexible IP
attaches to exactly one server, so a split `/64` could **never** complete its FIP
move — the tunnel would become permanent (the "lasting bridge" risk the design
exists to avoid). Forcing one target makes the FIP move always eventually
possible; draining a whole box onto fresh capacity is the natural unit anyway.

> **Surfaced exception — partial drain.** If the operator migrates *some* VMs and
> deliberately leaves others on the source, the FIP cannot move and the tunnel
> persists for the migrated VMs until the source fully drains. This is the **only**
> lasting-tunnel case and has **no automatic forcing function** (the operator must
> migrate or terminate the stragglers). It is therefore not silently allowed: it
> is surfaced as a first-class operator-visible state (`Server.pending_fip_move`
> + a dashboard indicator "/64 move pending: N VMs still on source"). **Open for
> the operator:** whether v1 should hard-reject a keep-address migration unless it
> will fully drain the source — recommended as a follow-up, not v1.

### 2.5 The `/64` move (one provider API pair) and the Server rows that change

When the drain predicate holds, a **deferred reconciler** (not any VM's Cleanup —
§2.6) performs the move via a new typed Provider method:

```python
# providers/base.py (ABC default raises NotImplementedError)
def move_flexible_ipv6(self, fip_id, target_server_resource_id): ...


# providers/scaleway.py
def move_flexible_ipv6(self, fip_id, target_server_resource_id):
	self.client.detach_flexible_ip(fip_id)  # waits for detach to settle
	self.client.attach_flexible_ip(fip_id, target_server_resource_id)
```

`detach_flexible_ip` already waits for the FIP to settle, so the immediate
re-attach isn't rejected; the pair is idempotent (the same shape
`_ensure_flexible_ipv6` already proves). A new `_flexible_ipv6_fip_id(server_id)`
helper (mirroring `_flexible_ipv6_range` but returning the UUID) supplies
`fip_id`.

**Server rows after the move:**

- `Server[target].ipv6_virtual_machine_range` → the moved `/64` CIDR.
- `Server[source].ipv6_virtual_machine_range` → **cleared (empty), not
  re-derived to the on-link prefix.** Writing the source's bundled on-link `/64`
  (SLAAC — the host's own subnet, *not* routed to the box) would make
  `allocate_ipv6` hand out `/128`s the edge won't route → every future VM on that
  box dark. A drained source simply has **no VM range** until `_ensure_flexible_ipv6`
  allocates it a fresh routed FIP at the next provision (idempotent).

**Target two-`/64`s wrinkle (v1 rule).** An Atlas-provisioned target already
holds its own flexible `/64`; the migrated VMs keep their **source** `/128`s, so a
naive move leaves the target holding two `/64`s while `allocate_ipv6` reads one
field. **v1 rule: keep-address drains onto an empty target** — pre-flight rejects
a target that has any non-Terminated VM on its own `/64`. On move, swap
`Server[target].ipv6_virtual_machine_range` to the migrated `/64` and **release
the target's original empty FIP** (`delete_flexible_ip`), so the target ends with
exactly one routed `/64`. Multi-range storage (a `Server Virtual Machine Range`
child table) is a **deferred follow-up**, noted but not built.

After the move, for each migrated VM the reconciler **finalizes** natively (the
`/128` is now ∈ the target's range — delivery is automatic via the moved FIP
route) and tears the transit down: remove the source's `dev mig6-` route + nft
rules and the target's `ip -6 rule from <vmv6>`, then `ip link del mig6-<fip8>` +
kill the `socat`/SSH-forward on both ends.

### 2.6 State-machine integration

The per-VM phase **order is unchanged** (§3). The branch is `doc.keep_address`
(set at insert, §2.8) **and** `not doc.forward_address` — this table is the
range-move sub-path (Scaleway); §2.9.4 has the equivalent table for the
permanent-forward sub-path (DigitalOcean). The `/64` move + tunnel teardown are
**not** in any per-VM phase — they live in a separate, deferred reconciler,
because they can only run once the *last* VM drains.

| Phase | change-address (Self-Managed fallback, §2.8) | keep-address, range-move (Scaleway) |
|---|---|---|
| `Pending` | as §4 | + drain-to-one and empty-target pre-flight (§2.4–2.5) |
| `ExportingSnapshot` | as §5 | as §5 |
| `TargetPreparing` | build dm-clone | + **create the block TUN on both ends** (`migration-tunnel-up.py`, §2.1) |
| `InjectingIdentity` | `allocate_ipv6(target)` → **new** `/128`, rewrite `network.env` | **near-no-op for networking.** `ipv6_address` unchanged → env, v4 `/30`, host keys already correct; copies address fields verbatim; still does any non-address inject (e.g. data-disk fstab). **No `allocate_ipv6`, no env rewrite.** |
| `Hydrating` | as §5 | as §5 |
| `CutoverStarting` | source unit down, target up | same, **then** target `migration-target-receive.py` (return route, §2.3) **then** source `migration-source-forward.py` (route→tunnel + nft, §2.2) |
| `Repointing` | `db_set Subdomain.address` + `reconcile_region` | **no Subdomain re-point, no `reconcile_region`** — shrinks to: flip `Virtual Machine.server` (under `flags.migrating`), `status = Running`; `ipv6_address` not written |
| `Cleanup` | source teardown as §5 | same source teardown, **but the tunnel + `dev mig6-` route + nft + return-rule stay** (they carry live traffic until the block drains). At the end: `arm_block_fip_move(source, target, fip_id)` (sets `Server.pending_fip_move`, idempotent) |

**Deferred move — a new scheduler entry `reconcile_block_fip_moves()`** (a `cron`
sibling of `reconcile_migrations`, ~2-min cadence, same try/except-per-server
idempotent shape). Each tick, for every `Server` with `pending_fip_move=1`:

1. Re-check the drain predicate (§2.4). Not drained → skip (the partial-drain hold).
2. Drained → `move_flexible_ipv6(...)` + rewrite both `Server` ranges + release
   the target's old empty FIP (§2.5).
3. Finalize each migrated VM natively + tear down the per-VM source route/nft and
   the target return-rule + delete the block tunnel on both ends. Clear
   `pending_fip_move`.

The arm-in-Cleanup / execute-in-reconciler split is the resumable shape: the
last VM's Cleanup can't know it's last without a race, but a reconciler
re-evaluating the DB predicate each tick is monotonic and self-healing.

### 2.7 New fields

**`Virtual Machine Migration`** (added to the JSON in §*Sample implementation*):

- `keep_address` — `Check`, `read_only`, `set_only_once`. `1` iff either
  keep-address mechanism applies (§2.8's capability check) — the branch switch
  for every row above, on **both** sub-paths. (`ipv6_address_new` stays empty
  on either keep path; `ipv6_address_old` is the live, unchanged address.)
- `forward_address` — `Check`, `read_only`, `set_only_once`. `1` iff the
  keep-address mechanism is specifically the **permanent-forward** path
  (§2.9, DigitalOcean) rather than the range-move path (§2.1–§2.7, Scaleway).
  Disambiguates the two `keep_address` sub-paths in the phase table (§2.6 vs.
  §2.9.4); always `0` when `keep_address` is `0`.
- `block_tunnel_device` — `Data`, `read_only`. The `mig6-<fip8>` (range-move) or
  `mig6-<vm8>` (permanent-forward) iface name (teardown / lost-task re-entry
  handle) — one field, populated by whichever sub-path is active.
- `block_fip_id` — `Data`, `read_only`. **Range-move only**: the source
  flexible-IP UUID this block drains. Empty on the permanent-forward path
  (there is no block).
- `tunnel_status` — `Select` (`\nArmed\nForwarding\nTornDown`), `read_only`.
  Observability of the live forwarding path. On the permanent-forward path this
  reaches `Forwarding` in `Cleanup` and **stays there** — `TornDown` is only
  reachable via the manual Collapse-forward action (§2.9.5), never
  automatically.
- `forward_active` — `Check`, `read_only`. Permanent-forward path only (§2.9.5):
  mirrors `tunnel_status == "Forwarding"` as a simpler predicate for the
  dashboard indicator and to gate the Collapse-forward button.

**`Server`** (the deferred `/64`-move state — a server-level fact, not per-VM;
**range-move (Scaleway) only** — the permanent-forward path has no block and
so touches none of these three fields):

- `pending_fip_move` — `Check`, `read_only`. Armed by a range-move keep-address
  migration's Cleanup; consumed by `reconcile_block_fip_moves`.
- `fip_move_target` — `Link → Server`, `read_only`. The single drain target
  (records and enforces §2.4's one-target rule).
- `fip_move_fip_id` — `Data`, `read_only`. The flexible-IP UUID to detach/attach.

(Rejected: a separate `Flexible IP Block Move` DocType — the move is 1:1 with a
source server, so three `Server` fields + the reconciler *are* the state machine.
Promote to a child table only if multi-block-per-server lands.)

### 2.8 Self-Managed fallback + portability detection

**Self-Managed falls back to the change-address path** unless an operator wires
BGP re-announce (out of scope): `allocate_ipv6(target)` → new `/128`, rewrite
`network.env` in `InjectingIdentity`, `db_set` every `Subdomain.address` +
`reconcile_region` in `Repointing` (§3). **No tunnel, no FIP move, no
source-forward.** This is the same path Scaleway used to fall back to before a
movable FIP was verified present, and the same shape a future Self-Managed BGP
capability would escape by returning `True` below.

**Detection is a pair of Provider capability methods, never a
`provider == "..."` literal** (matching the ABC's concrete-default pattern,
e.g. `prepare_host`). There are now **two independent keep-address mechanisms**
(§2.1–§2.7's range-move, §2.9's permanent-forward), so there are two capability
bits — a provider may have neither, either, or (hypothetically) both:

```python
# providers/base.py — both default False (Self-Managed, Fake)
def vm_range_is_portable(self, provider_resource_id) -> bool:
	"""True iff this server's ipv6_virtual_machine_range can be moved, whole,
	to another server via the provider API (the range-move keep-address path,
	§2.1-2.7)."""
	return False


def vm_range_is_forwardable(self, provider_resource_id) -> bool:
	"""True iff this server's per-VM addresses are delivered by a mechanism
	(e.g. proxy-NDP) that a *different* host can permanently answer on the
	VM's behalf, once told to — the permanent-forward keep-address path
	(§2.9). Unlike vm_range_is_portable, this needs no provider API call: it
	models a fact about the delivery mechanism, not a movable resource."""
	return False


# providers/scaleway.py — True only if a movable FIP actually exists
def vm_range_is_portable(self, provider_resource_id):
	return bool(self._flexible_ipv6_range(provider_resource_id))


# providers/digitalocean.py — proxy-NDP delivery always supports forwarding
def vm_range_is_forwardable(self, provider_resource_id):
	return True
```

At migration insert:
```python
range_portable = get_provider().vm_range_is_portable(
	source.provider_resource_id
) and get_provider().vm_range_is_portable(target.provider_resource_id)

range_forwardable = get_provider().vm_range_is_forwardable(
	source.provider_resource_id
) and get_provider().vm_range_is_forwardable(target.provider_resource_id)

keep_address = range_portable or range_forwardable
forward_address = keep_address and not range_portable  # the §2.9 branch specifically
```
Scaleway's `vm_range_is_portable` override returns `True` only when the box
really holds a movable FIP (a Scaleway box whose FIP allocation failed
correctly falls back to change-address); DigitalOcean's `vm_range_is_forwardable`
override returns `True` unconditionally (proxy-NDP delivery is how every DO
droplet works, not a fact that can fail per-box). The gate is on the
*capability*, not the provider name — a future Self-Managed BGP path flips its
own `vm_range_is_portable` bit, and a future provider whose per-VM delivery is
also proxy-NDP-like inherits §2.9 by flipping `vm_range_is_forwardable`,
without touching migration code either way.

### 2.9 Permanent per-VM forwarding (DigitalOcean keep-address path)

DigitalOcean's `/124` is carved locally around each droplet's own address
([06](./06-networking.md)) and cannot be moved — there is no block to drain, no
FIP to reattach, nothing resembling §2.1–§2.7's range-move. But the `/128` can
still be kept fixed, because DO's delivery mechanism is different in a way that
makes this *easier*, not harder: DO's edge finds the right box by **NDP on the
droplet's uplink**, not by a routed prefix. The source droplet can keep
answering that NDP forever and quietly forward the matched traffic to wherever
the VM actually lives. No range needs to move; only the destination of one
`/128`'s traffic does.

**Scope: today only, per §1.** This section assumes exactly the operating
context the operator has set: DigitalOcean is a fast, non-production dev
platform here, so **the source droplet is never decommissioned** and there is
no requirement to ever reclaim the forward. If that changes later (DO promoted
to a real target, sources needing archival), the design would need a drain/
release story analogous to §2.4–§2.6; that is explicitly **not** built now —
see the *Open follow-up* at the end of this section.

#### 2.9.0 What's reused from §2.1, and what's not

The **tunnel carrier** is identical to §2.1: an SSH-forwarded `socat`-bridged
`tun` device (no new public port, no proto-41/47 hole — the same
[06](./06-networking.md) constraint applies here too). Everything about *why*
that shape was chosen (§2.1) carries over verbatim.

Everything **keyed to the block** does not carry over, because DO has no
block: there is no shared `/64` FIP multiple VMs drain together, so there is no
drain-to-one-target rule (§2.4), no deferred `/64` move (§2.5), no
`Server.pending_fip_move` (§2.7). The tunnel is **keyed to the VM**, one device
per migrated VM, brought up once and left up.

#### 2.9.1 The tunnel — one `tun` device per migrated VM

**Naming — keyed to the VM's own UUID, not a block.** Device name
`mig6-<first 8 hex of the VM's UUID>` (mirrors `derive_tap`'s discipline, reuses
the ≤15-char IFNAMSIZ-safe scheme). New pure helper in `networking.py`:
`derive_vm_tunnel(virtual_machine_name)` (the iface name) and
`derive_vm_tunnel_port(virtual_machine_name)` (a per-VM localhost port, same
derivation shape as `_nbd_port`, in a range that doesn't collide with
`derive_block_tunnel_port`'s).

A typed `migration-forward-up.py` Task brings it up on **both** ends in
`TargetPreparing` (idempotent). Source side: `socat
TCP4-LISTEN:<port>,bind=127.0.0.1,reuseaddr,fork TUN,tun-name=mig6-<vm8>,iff-up,iff-no-pi`,
then `ip -6 addr replace fe80::a/64 dev mig6-<vm8> nodad; ip link set … up`.
Target side: SSH `LocalForward` for `<port>` (idempotent, exactly like NBD),
then `socat TCP4:127.0.0.1:<port>,retry,forever TUN,tun-name=mig6-<vm8>,…` +
`ip -6 addr replace fe80::b/64 dev … ; ip link set … up`. Same MTU caveat as
§2.1 (pin to 1280, host-probe throughput before relying on it in production) —
carried over verbatim, not re-litigated.

#### 2.9.2 Source side — keep the proxy-NDP entry, replace the delivery route

On DO, `vm-network-up.py` adds `ip -6 neigh replace proxy <vm-ip> dev <uplink>`
independent of the VM's netns/veth/tap ([06](./06-networking.md),
`scripts/vm-network-up.py`), and `vm-network-down.py` removes it
(`check=False`, best-effort, like every teardown step in that script). At
cutover, the source unit's own `ExecStopPost` (`vm-network-down.py`) runs when
it is disabled and deletes this entry along with the rest of that VM's
networking — the standard, unconditional teardown, same as any migration's
source-side unit stop. Left alone, the address would go dark the moment DO's
edge next asks "who has this `/128`?".

**§2.9-specific: `CutoverStarting` re-asserts the proxy-NDP entry immediately
after the source unit's stop deletes it**, via a typed
`migration-source-forward.py` Task that also points delivery at the tunnel
instead of the now-torn-down veth:

```
ip -6 neigh replace proxy <vmv6> dev <uplink>
ip -6 route replace <vmv6>/128 dev mig6-<vm8>
nft add rule inet atlas forward ip6 daddr <vmv6> oifname mig6-<vm8> accept
nft add rule inet atlas forward ip6 saddr <vmv6> iifname mig6-<vm8> accept
```

This is the same atomic-replace, no-black-hole shape as §2.2. Unlike §2.2, the
proxy-NDP entry itself is new state this phase re-creates (§2.2's Scaleway path
never needed one — routed delivery has no NDP step); everything downstream of
that entry is otherwise identical in shape.

#### 2.9.3 Target side — normal `vm-network-up`, plus a return route

**Open, not yet host-verified: does DO's edge drop egress sourced from an
address outside the droplet's own `/64`?** This is the DO analog of §2.0's
BCP38 question, and it is **not** the same question with the same answer
already assumed — DO's positive case ([06](./06-networking.md)'s reserved-IP
anchor-NAT path) only establishes that a droplet may source its *own* bound
addresses; nothing in this codebase or DO's public docs has been checked
against a droplet sourcing an address that belongs to a **different**
droplet's `/64`. Until host-verified, this design takes the same conservative
branch §2.0 takes for Scaleway: **assume it is dropped**, and route the return
path back through the source, symmetric with §2.3.

The target runs its **normal `vm-network-up.py`** at unit start — same netns/
veth/tap, same `<vmv6>/128 via fe80::3 dev <host_veth>` route. **No change to
`vm-network-up.py`.** The one addition (`migration-target-receive.py`, same
phase as §2.9.2, run **before** it so the return path exists first) is the
return-route policy:

```
ip -6 rule add from <vmv6> lookup <vm_table> priority 100
ip -6 route replace default dev mig6-<vm8> table <vm_table>
```

`<vm_table>` is a small fixed table id derived from the VM's UUID (one rule per
migrated VM — there is no block to share it across, unlike §2.3's
`<block_table>`). Inbound: source answers NDP → tunnel → target veth → guest.
Outbound: guest → veth → **policy-routed to the tunnel** → source → source's
own uplink, sourced from an address the source's own `/64` legitimately owns.
If a future host probe shows the target *can* egress the source's range
directly, this return route becomes unnecessary — a strictly easier case, exactly
as §2.0 notes for Scaleway.

#### 2.9.4 State-machine integration

The per-VM phase **order is unchanged** (§3). The branch is `doc.keep_address`
with `doc.forward_address` (§2.8) set — same row-level flag namespace as
Scaleway's `keep_address`, disambiguated by which sub-mechanism applies.

| Phase | DigitalOcean permanent-forward | (for contrast) Scaleway range-move |
|---|---|---|
| `Pending` | as §4 — no drain/empty-target checks, there is no block | + drain-to-one and empty-target pre-flight (§2.4–2.5) |
| `ExportingSnapshot` | as §5 | as §5 |
| `TargetPreparing` | build dm-clone + **create the per-VM tunnel** (`migration-forward-up.py`, §2.9.1) | build dm-clone + create the block TUN (§2.1) |
| `InjectingIdentity` | near-no-op for networking, same as Scaleway's row in §2.6: address fields copied verbatim, no `allocate_ipv6`, no env rewrite | same |
| `Hydrating` | as §5 | as §5 |
| `CutoverStarting` | source unit down, target up; target `migration-target-receive.py` (§2.9.3) **then** source **re-asserts proxy-NDP** + `migration-source-forward.py` (§2.9.2) | target return-route then source-forward (§2.2–2.3) |
| `Repointing` | no Subdomain re-point, no `reconcile_region` — flips `Virtual Machine.server` (`flags.migrating`), `status = Running`; `ipv6_address` not written | same |
| `Cleanup` | source teardown as §5, **except the proxy-NDP entry, the tunnel, the `dev mig6-` route, the nft rules, and the target return-rule are never torn down** — they are the permanent delivery path now. Sets `Virtual Machine Migration.forward_active = 1` (observability, §2.9.5) | same teardown, but arms the deferred `/64` move instead (§2.6) |

There is **no deferred reconciler** for this path — no `reconcile_block_fip_moves`
analog, because there is nothing to converge toward. The forward is simply part
of the migrated VM's permanent shape from `Cleanup` onward, the same way its
UUID-derived MAC/TAP/netns are permanent.

#### 2.9.5 Operator visibility and manual teardown

Because the forward is permanent by default (§2.9 scope, above) but the
operator should not be surprised by an invisible cross-host dependency, the
migration row and the VM form both surface it, and provide a way to remove it
by hand once the operator is ready (e.g. they decide the source droplet really
is going away, or they simply want to collapse the extra hop):

- `Virtual Machine Migration.forward_active` — `Check`, `read_only`. Set by
  `Cleanup` (§2.9.4); mirrors `tunnel_status`'s observability role but stays `1`
  indefinitely rather than progressing to `TornDown`.
- A **dashboard indicator** on the migrated `Virtual Machine` — "Traffic
  forwarded from `<source>` since `<date>`" — for as long as `forward_active`
  is set, so the fact is visible on the VM itself, not only buried in a
  migration-history row.
- A manual **"Collapse forward"** action (button + whitelisted method), enabled
  only while `forward_active`, that runs the inverse of §2.9.2–§2.9.3 by hand:
  tear down the source's proxy-NDP entry, `dev mig6-` route, and nft rules; tear
  down the target's return-rule; delete the tunnel on both ends; fall back to
  **change-address** semantics from that point — `allocate_ipv6(target)` a new
  `/128`, rewrite `network.env`, `db_set` every `Subdomain.address` +
  `reconcile_region`, exactly as §3's `Repointing` does for the plain
  change-address path. This is the **only** point at which a DO-migrated VM's
  address can still change, and it is entirely operator-initiated — never
  automatic, since (per this section's scope) nothing forces it.

> **Open follow-up, not v1.** If DO ever needs a source-decommission story
> (multiple VMs forwarding off one droplet the operator wants to retire), the
> natural shape is a block-level "collapse all forwards from this source"
> sweep of the Collapse-forward action above, run once per VM. It is *not* the
> single-FIP-move Scaleway has (there is no DO API for that), so it would
> always cost one change-address migration per forwarded VM, never a single
> free `/64` re-point. Deliberately not designed further here since the
> operating context (§2.9, above) doesn't call for it yet.

## 3. States

The `Virtual Machine` itself stays `Stopped` throughout (it only flips to
`Running` at cutover). The phase machine lives on the **migration row**:

```
 Pending
   │ (pre-flight passes; VM stopped, mem-snapshot cleared)
   ▼
 ExportingSnapshot ── source: thin-snap both LVs, start NBD on localhost:port
   ▼
 TargetPreparing ──── target: pre-flight image+pool+modules, create thin LVs,
   │                          open SSH tunnel + nbd client, dmsetup create …clone;
   │                          keep-address: also create the block TUN both ends (§2.1)
   ▼
 InjectingIdentity ── target: mount LV; change-address: inject new v6/v4 env;
   │                          keep-address: networking near-no-op (address unchanged);
   │                          both: keep host keys
   ▼
 Hydrating ───────────target: enable_hydration once; scheduler re-probes % each
   │                          tick; advance at 100%, Fail on stall
   ▼
 CutoverStarting ──── source unit down; target unit up; poll Running+SSH;
   │                  collapse dm-clone; keep-address: target return-route then
   │                  source-forward onto the tunnel (§2.2–2.3)
   ▼
 Repointing ───────── controller: commit row (server [+ ipv6 on change-address],
   │                  Running); change-address: db_set Subdomain addresses +
   │                  reconcile_region; Reserved-IP handling (§6)
   ▼
 Cleanup ──────────── source: kill NBD, lvremove migrate-snapshots, tear down
   │                  the stale source copy (old dir/LVs/netns); range-move
   │                  keep-address: leave the tunnel up + arm the deferred /64
   │                  move (§2.6); permanent-forward keep-address: leave the
   │                  tunnel + proxy-NDP up for good, no arming (§2.9.4)
   ▼
 Done                 terminal

 (any phase) ──► Failed   error_message set; Retry re-enters the last
                          non-Done phase (all idempotent); Rollback before
                          CutoverStarting just restarts the intact source VM
```

**Why this order is safe.** The source VM is never destroyed until `Cleanup`,
*after* the target is confirmed `Running` and routing is re-pointed. Any failure
through `CutoverStarting` rolls back by simply starting the source VM again — its
disk and `/128` are untouched (on the keep-address path the `/128` never moved at
all). `Subdomain` rows are only rewritten in `Repointing` (change-address only),
the point of no return.

## 4. Pre-flight (the `Pending` gate)

`migrate()` and the first phase refuse to proceed unless:

- target `Server.status == "Active"` and SSH-reachable (else: bootstrap it first);
- target and source are the **same provider** (cross-provider is out of scope);
  region is same by construction (one region per Atlas instance — §1);
- the VM `status == "Stopped"` (a `Running`/`Paused` VM is stopped first, with a
  **plain** stop — never `snapshot-stop-vm.py`; a captured RAM image is worthless
  on the target, so `has_memory_snapshot` is forced to 0 and the `snapshot/` dir
  is dropped);
- the base image is present on the **target** (checked on-host: the
  `atlas-image-<image>` base LV and the kernel file must exist — the same probe
  `provision-vm.py` does). **Two cases by image kind:**
  - **Syncable image** (has a rootfs URL): if absent on the target, `sync-image`
    is a separate multi-minute Task, so we fail loud and early, not late in boot.
  - **Local image** (`is_local` — promoted from a snapshot, no rootfs URL, so
    `sync-image` cannot place it): it lives only on the source host and is
    **shipped to the target during `TargetPreparing`** over the same NBD path the
    disk uses (§5.1). This does *not* fail pre-flight — a VM on a snapshot-promoted
    image is migratable;
- **change-address only:** the target has **IPv6 capacity**
  (`allocate_ipv6(target)` would succeed); **thin-pool headroom**
  (`PoolUsage` below the fill threshold) is checked on every path regardless of
  address scheme;
- **range-move keep-address only (Scaleway):** the drain-to-one-target rule
  holds and the target's own `/64` is empty (§2.4–2.5);
- **permanent-forward keep-address only (DigitalOcean, §2.9):** no range check
  needed (there is no block) — the only addition is that the per-VM tunnel
  port/table derivations (§2.9.1, §2.9.3) don't collide with another in-flight
  forward on the same source or target, mirroring the NBD-port collision
  avoidance already done for concurrent migrations;
- the VM's attached Reserved IP (if any) is handled per §6 — preserved across the
  move, or explicitly released with `release_reserved_ip=True`.

## 5. Storage: NBD export + dm-clone hydration

**Whole storage path VERIFIED end-to-end on real hosts (2026-07-02).** On the two
Scaleway Elastic Metal hosts: NBD export → dm-clone hydrate → collapse, run against a
**real 4 GiB ext4 VM disk** (a thin clone of the production image), gives a
**byte-identical** destination thin LV that **mounts cleanly** as the full Ubuntu
rootfs after collapse, with the ext4 UUID/LABEL preserved. Same flow also verified
**cross-host** (target on one host, `qemu-nbd` on the other) over the public IPv6
path at ~490 MiB/s. Three concrete impl requirements surfaced and are folded into the
steps below: (a) `qemu-nbd --persistent`, (b) parse `dmsetup status` positional
field 7, (c) the dest LV cannot be mounted until the clone is collapsed.

**Hydration acceptance (resolved).** The target **boots at any hydration %**
(reading through to the source over NBD), but the source thin snapshot and NBD
export are **held alive until hydration hits 100%**, and `Cleanup` runs **only
after** the dm-clone collapses. This gives fast availability *and* a clean
rollback window — the source VM and its disk stay intact and re-startable through
the entire `CutoverStarting` phase.

### Source side (`migration-export-source.py`, phase `ExportingSnapshot`)

1. Pre-flight `PoolUsage.too_full_to_snapshot` (`scripts/lib/atlas/lvm.py`).
2. Take a thin CoW snapshot of the **Stopped** VM's root LV into
   `atlas-snap-<uuid>-migrate`, and — if the VM has a data disk — its data LV
   into `atlas-datasnap-<uuid>-migrate`. A Stopped VM's filesystems are cleanly
   unmounted, so the snapshot is flush-clean and (with two disks) mutually
   consistent. (`ThinPool.snapshot_into`, idempotent — re-activates if present.)
3. Start `qemu-nbd` **bound to `127.0.0.1`** on a port derived from the UUID
   (`10000 + int(uuid.hex[:4],16) % 5000`, avoids collisions under concurrent
   migrations on one source), exporting the snapshot(s) **read-only**. With a
   data disk, a **second** `qemu-nbd` serves it on `port+1` (§*Data disk*).
   **Pass `--persistent` (verified-required):** without it `qemu-nbd` exits after
   the first client disconnect, which would break the "held alive until hydration
   100%" guarantee above (a dm-clone that re-reads after a transient nbd blip would
   find the server gone). Use `--fork` so the launch call returns once the socket is
   ready. (`--shared=N` alone does **not** keep it alive past disconnect.)
4. Emit `ATLAS_RESULT` with the port, the NBD pid, and each snapshot's
   `blockdev --getsize64`. The controller records `nbd_port` / `nbd_pid` on the row.

### Target side (`migration-clone-target.py`, phases `TargetPreparing` + `InjectingIdentity`)

1. Pre-flight: `dm_clone` + `nbd` kernel modules present (`modprobe`), `qemu-nbd`
   + `nbd-client` userspace present, the base image LV exists, thin-pool has
   headroom. (These deps now ship at bootstrap — §*New dependencies* — so this is
   a defensive re-assert, not the first install.)
2. Open an SSH `LocalForward` tunnel **from the target to the source**, so the
   NBD export is reachable at `127.0.0.1:<port>` on the target with **no public
   NBD port and no firewall hole**. Connect `/dev/nbdN`. This is host-to-host
   SSH initiated *by the target*, not controller→host — see the open
   credential question in §2.1, which applies here too (build order stage 1
   needs it already, not only the keep-address stages).
3. Create the fresh thin LV `atlas-vm-<uuid>` (size = the VM's `disk_gigabytes`,
   ≥ source) and, if needed, `atlas-data-<uuid>` (`ThinPool.create_thin`,
   idempotent). Create a small zeroed metadata LV `atlas-clonemeta-<uuid>`.
4. `dmsetup create atlas-vm-<uuid>-clone --table
   "0 <sectors> clone <meta> <dest-thin> <nbd-src> <region_sectors>"`
   (default region 16 MiB = 32768 sectors; tunable). Idempotent: skip if the
   mapper device already exists with the expected table.
5. **Identity inject** into the migrated disk before any boot: mount the
   **clone mapper device** `/dev/mapper/atlas-vm-<uuid>-clone` — **not** the bare
   `atlas-vm-<uuid>` thin LV, which is held open by the clone and would fail to
   mount `busy` (verified 2026-07-02). Writes through the clone land on the dest and
   count toward hydration; the plain LV is only mountable after collapse (§5.4).
   **change-address** rewrites `/etc/atlas-network.env` with the new
   `/128`, the new NAT44 `/30`, the v4 gateway, and the data-disk fstab line;
   **keep-address** leaves the network env untouched (the `/128` is unchanged) and
   only writes any non-address bits. Both via
   `rootfs.inject_identity(device, Identity(...), regenerate_host_keys=False)` —
   **host keys preserved**, exactly as `rebuild`/`restore` do. Unmount.

### 5.1 Local base image ship (`migration-export-base.py` + `migration-receive-base.py`)

A VM whose base image is **local** (`is_local` — promoted from a snapshot, no
rootfs URL; spec/08-images.md) cannot be migrated by the flow above as-is: the
`atlas-image-<image>` base LV lives **only on the source host** it was promoted
on, and `sync-image` has nothing to download. Before this, such a VM wedged
`TargetPreparing` at `base image LV not on target: …; run Sync to Server first`.

The fix ships the local base to the target **the same way the disk is shipped** —
an NBD export the target flattens into a fresh local LV — so no new transport, no
host-to-host SSH (deferred, §2.1), no HTTP surface. It runs as the **first step of
`TargetPreparing`**, before the disk clone, and is a no-op for a syncable/already-
present image. A base image needs **two** artifacts on the target, so there are two
exports over the disk export's spare ports:

- **port `nbd_port+2`** — the read-only `atlas-image-<image>` LV as a block export
  (`migration-export-base.py`). The base is immutable, so it is exported
  **directly**, with no thin snapshot in between (unlike the live VM disk).
- **port `nbd_port+3`** — a **file-backed** NBD export of a `tar` of the image
  directory (`kernel` + rootfs sentinel). `qemu-nbd` serves a plain file, so the
  small metadata tar rides the same channel — this is how the **kernel** reaches
  the target without a controller→host download or host-to-host copy.

Target (`migration-receive-base.py`), two phases driven per scheduler tick:

1. `PHASE=prepare` — `nbd-client` to both exports (slots 8/9, clear of the disk
   clone's 0/1); create the writable thin LV `atlas-image-<image>`; build a
   dm-clone `atlas-base-<image>-clone` reading through the base export; extract the
   image-dir tar into `image_directory(<image>)`.
2. controller enables + polls hydration via `migration-poll-hydration.py`
   **`--clone-device atlas-base-<image>-clone`** (the same script + percent parse
   as the VM disk — this is why the poll script grew an explicit `clone_device`),
   writing `base_ship_percent`/`progress_percent` each tick so the copy is visible.
3. `PHASE=finalize` — at 100%, collapse the dm-clone to the plain LV, `lvchange
   --permission r` (a base image is never written), disconnect the nbd clients.
   The result is a first-class local base image on the target, indistinguishable
   from a synced one; the migration then proceeds to the normal disk clone.

`TargetPreparing` therefore becomes **non-advancing while a base ships** (returns
False to re-enter), exactly like `Hydrating`. Cleanup (`migration-cleanup-source`)
kills the `+2`/`+3` exports and removes the staged tar; the source's base LV is its
own immutable image and is **never** removed.

### Data disk (resolved: a second parallel dm-clone)

A data disk migrates as a **second dm-clone over a second NBD export** (root =
`nbd_port`, data = `nbd_port+1`), symmetric with the root disk: same idempotency
+ hydration-poll machinery, and the data disk is **available immediately** too
(boot-and-hydrate holds for both disks). The blocking-`dd` alternative was
rejected — it would leave the data disk unusable until the copy finished.

### Hydration (`migration-poll-hydration.py`, phase `Hydrating`)

`dmsetup message <dev> 0 enable_hydration` **once** (per disk), then the
**scheduler** re-enters this phase each tick and runs a *short, read-only* status
probe, recording `hydration_percent` (the min across both disks). Only at 100% does
it advance. **Parsing note (verified):** `dmsetup status` emits **no `hydration`
label** — the real line is `0 <len> clone <meta_bsz> <mused>/<mtot> <region_sz>
<hydrated>/<total> <hydrating> <feature_args…>`, so the poll must read the
`<hydrated>/<total>` pair from **positional field 7** (1-indexed), not grep for a
keyword. dm-clone also pre-marks a few regions hydrated at create time (zero/discard
regions) before `enable_hydration`; the destination is still byte-identical at 100%.
This keeps a multi-minute hydration **off the worker** — the long wait is a
sequence of cheap polls, not a held job. Stall guard: if the percentage is
unchanged for N ticks, the migration goes `Failed`.

### Collapse + cutover (`migration-cutover-target.py`, phase `CutoverStarting`)

Disable the source unit defensively (`systemctl disable --now
firecracker-vm@<uuid>`), start the target unit, poll until `Running` and
SSH-reachable. After cutover each dm-clone is collapsed (`dmsetup remove` once
its hydration is 100% — reads now serve from the fully-local thin LV) and the
jail's `rootfs.ext4` node points at the plain `atlas-vm-<uuid>`. On the
keep-address path this is where the target return-route and the source forward
are installed (§2.2–2.3).

### Cleanup (`migration-cleanup-source.py`, phase `Cleanup`)

Kill the NBD server(s) (by recorded pid, no-op if gone), `lvremove` both
`-migrate` snapshots (guarded by `LogicalVolume.remove()` against base images),
and tear down the **stale source copy** — the old per-VM directory, LVs, netns,
veth, proxy-NDP entry — with the same teardown `terminate-vm.py` performs, but
against the *old* host. This proxy-NDP removal already happened as part of the
source unit's own stop (`vm-network-down.py`, `ExecStopPost`) before `Cleanup`
even starts, on every path — so `Cleanup` itself has nothing v6-specific left to
remove except:

- **range-move keep-address (Scaleway):** the block tunnel + source forward
  route + nft + target return-rule are **left in place** (they carry live
  traffic until the `/64` drains — §2.6);
- **permanent-forward keep-address (DigitalOcean, §2.9):** the proxy-NDP entry
  (re-asserted in `CutoverStarting`, §2.9.2) + the per-VM tunnel + nft rules +
  target return-rule are **left in place permanently** — there is no drain
  condition to wait for (§2.9.4).

If any step fails the row stays at `Cleanup` with explicit manual-recovery
guidance in `error_message`: there is **no orphaned-LV reconciler**, so the
row's visibility *is* the backstop (consistent with
[18](./18-bench-self-routing.md)'s "no sweeper" stance).

## 6. Reserved IP (public IPv4)

Resolved: the customer's inbound v4 **survives the move** by reassigning the
vendor Reserved IP to the target droplet and repointing the row — made possible
by relaxing `Reserved IP.server` immutability ([02 § Reserved IP](./02-doctypes.md#reserved-ip)).
`Reserved IP.server` is **no longer immutable**: the IP is bound to its address +
vendor handle for life, but which Server it points at is a mutable pointer, and
an IP may rest with **no Server** at all (allocated-on-the-vendor). The new
`reassign(target_server)` method moves the IP at the vendor and repoints the row.

So `Repointing` handles an attached Reserved IP by:

1. `detach()` it from the VM on the source (clears the host 1:1-NAT there — the
   VM is Stopped at that instant, then Running on the target);
2. `reassign(target_server)` — `assign_reserved_ip(handle, target_droplet)` at the
   vendor + repoint `Reserved IP.server`;
3. `attach(virtual_machine)` it to the migrated VM on the target (re-applies the
   host 1:1-NAT on the new host).

The IP, and any DNS A record pointed at it, are unchanged across the move. The
pre-flight still accepts `release_reserved_ip=True` as an explicit **drop-it**
override (for the operator who wants the address freed rather than moved); absent
that flag, the default is now **preserve**, not drop. (Self-Managed has no vendor
bind, so `reassign` only repoints the row and the operator re-routes the address.)

## 7. The callback: resumability across API issues and rate limits

`reconcile_migrations()` is a `scheduler_events` `cron` entry (~every 2 minutes;
note the existing TLS-renew entry is `daily`, this is a new `cron` block in
`hooks.py`). It selects every non-terminal `Virtual Machine Migration` and calls
`advance_migration(row)` inside a **try/except per row** — one stuck migration
never blocks the others, and a failure marks just that row `Failed`.
`advance_migration` reads `status`, checks the phase's **idempotency key** ("am I
already done?"), and — if not — runs the phase's Task **inline via `run_task`**
(not `frappe.enqueue`: inline avoids the lost-worker-job failure class entirely,
and `run_task` saves the Task row first and raises on failure). This is the same
resilience shape as the provider worker's `finish_provisioning`
(`atlas/atlas/providers/worker.py`) and the documented "lost RQ job → re-run
inline idempotently" recovery. The deferred `/64` move runs under its own
`cron` sibling, `reconcile_block_fip_moves()` (§2.6).

A phase Task that has been `Running`/`Pending` past `2×` its timeout is treated
as **lost** and the phase is re-entered idempotently — recorded, never a silent
duplicate.

## 8. Operator UX (resolved: one button + scheduler)

One **Migrate** button on the `Virtual Machine` form creates the
`Virtual Machine Migration` row (a target-server picker + the optional
`release_reserved_ip` ack); the scheduler drives it. The Migration form shows the
phase pill, the hydration %, the `tunnel_status`, and a **Retry** on `Failed`.
Per-phase manual buttons exist only as an optional debug affordance, not the
primary flow — the lifecycle guard (`_guard_no_active_migration`) blocks
concurrent lifecycle actions on a VM mid-migration regardless.

## 9. Build order (staged rollout)

The design above (§1–§8) is the **end state**. It is built in four stages, each
one a working, mergeable increment that narrows the gap to the end state
rather than throwaway scaffolding — every stage's code survives into the next
because change-address and keep-address are already parallel branches of the
*same* phase table (§2.6), not two different designs.

1. **Change-address only, IP always changes.** Build §3–§7 with `keep_address`
   hard-forced to `0` (the `vm_range_is_portable` / `vm_range_is_forwardable`
   capability checks from §2.8 are not wired in yet — every migration takes the
   change-address column of §2.6's table). This alone proves the hard part of
   *moving a stopped VM*: NBD export, dm-clone hydration, identity injection,
   cutover, and — critically — that the UUID, SSH host keys, disk contents, and
   `Subdomain` links all survive the move onto a new `server` row. No proxy
   changes yet; a migrated VM's address changes, so anything still dialing the
   old `/128` breaks — acceptable because nothing is repointed to depend on it
   yet. Verifiable in isolation: migrate a VM with no `Subdomain`, diff its disk
   and identity before/after.
2. **Proxy repoint.** Add §1's `Repointing`-phase Subdomain/proxy half:
   `db_set Subdomain.address` + `reconcile_region` (already fully specified in
   §2.6's change-address column and §3). This is the first stage where a
   **site or bench** can migrate cleanly end-to-end — the address changed in
   stage 1, but nothing followed it there until now. Verifiable: migrate a VM
   with an attached `Subdomain`/bench and confirm the site is reachable at the
   same hostname post-migration with zero manual proxy intervention.
3. **Fix the networking — keep-address.** Build all of §2: the TUN tunnel
   (§2.1), source-forward and target-receive routes (§2.2–2.3), the block-drain
   rule and deferred `/64` move (§2.4–2.6, Scaleway), and permanent per-VM
   forwarding (§2.9, DigitalOcean). Wire in the real `vm_range_is_portable` /
   `vm_range_is_forwardable` capability checks (§2.8) so `keep_address` is now
   computed, not forced. The VM's `/128` now survives a migration on both
   supported providers; `server` becomes the only field `Repointing` changes on
   that path.
4. **Remove the repoint on the keep-address path.** Once stage 3 is proven, the
   stage-2 Subdomain/proxy rewrite is provably dead weight for every migration
   that takes a keep-address branch — the address never changed, so the rows
   were already correct (§1's point 3). This is already reflected in §2.6's
   table (the keep-address column skips `Repointing`'s Subdomain writes
   entirely) — this stage is where that skip actually starts executing, by
   flipping stage-3's capability checks on for real traffic. The
   change-address code from stage 2 is **not deleted**: it remains the
   Self-Managed fallback (§2.8) and the DigitalOcean Collapse-forward escape
   hatch (§2.9.5), both of which still need it.

Each stage's E2E scenario (§*Testing*) is a strict superset of the previous
stage's: stage 1 is the "all three" bullet minus the address-unchanged
assertions; stage 2 is the full change-address scenario; stage 3 adds the two
keep-address scenarios; stage 4 adds no new scenario, it *removes* Subdomain
writes from an existing one and asserts they no longer happen.

## New dependencies

The cold-migration disk move needs `qemu-nbd`/`nbd-client` (userspace) and the
`nbd` + `dm_clone` **kernel modules**. These are now **folded into
`bootstrap-server.py`** so every Active host can be a migration source or target
without a re-bootstrap: `qemu-utils` + `nbd-client` + `socat` (the §2.1 tunnel
carrier) join the apt set — all three **verified to install cleanly on a real
production Ubuntu 24.04 host with no disruption to its running VMs** (2026-07-02) —
and a dedicated step installs `linux-modules-extra-$(uname -r)` (version-pinned to the
running kernel — never the floating `-generic` metapackage) and loads + persists
`nbd` and `dm_clone` via `/etc/modules-load.d/60-atlas-migration.conf`, mirroring
`dm_thin_pool`. `CONFIG_DM_CLONE` merged in kernel 6.4; Ubuntu 24.04 ships 6.8, so
the module is present once the extra package is on. [README § principle 5](./README.md)
and [03-bootstrapping.md](./03-bootstrapping.md) are updated to match. The target
clone-script still defensively re-asserts the modules in its pre-flight (a clear
"re-bootstrap" message if a host predates this change), but it is no longer the
first install.

## Sample implementation

See [`spec/samples/migration/`](./samples/migration/) (drafted alongside this
spec, illustrative — not committed app code):

- `virtual_machine_migration.json` / `.py` — the new doctype + controller.
- `virtual_machine_migrate_patch.py` — the focused `virtual_machine.py` changes
  (the `flags.migrating` gate, `migrate()`, the lifecycle guard).
- `migration.py` — the `reconcile_migrations` callback + `advance_migration`
  phase dispatcher.
- `migration-export-source.py`, `migration-clone-target.py`,
  `migration-poll-hydration.py` — representative typed scripts.

> The sample predates the §2 keep-address design and the §6 Reserved-IP-preserve
> decision: it shows the change-address path and a detach-and-drop Reserved IP.
> The keep-address tunnel scripts (`migration-tunnel-up.py`,
> `migration-source-forward.py`, `migration-target-receive.py`), the
> `keep_address` branch, `reconcile_block_fip_moves`, and the Provider
> `vm_range_is_portable` / `move_flexible_ipv6` methods are specified in §2 but
> not yet sampled. The §2.9 permanent-forward path (`migration-forward-up.py`,
> the reused `migration-source-forward.py`/`migration-target-receive.py` under
> their DO-specific commands, `forward_address`, `vm_range_is_forwardable`, the
> Collapse-forward action) is likewise specified but not sampled. Build from the
> spec, not the sample, where they differ.

## Testing (when built)

- **Unit** (`test_virtual_machine_migration.py`): the `flags.migrating`
  immutability exception; the per-VM single-migration guard; the lifecycle
  guards; each phase's idempotency key; lost-Task re-entry; the change-address
  Subdomain re-point via `db_set` + `reconcile_region`; the keep-address branch
  (no re-point, address copied) on **both** sub-paths; the `vm_range_is_portable`
  and `vm_range_is_forwardable` gates and the `keep_address`/`forward_address`
  derivation from them; the range-move drain-to-one and empty-target pre-flight
  throws (Scaleway); the permanent-forward path never touching `Server`'s
  `pending_fip_move`/`fip_move_*` fields (DigitalOcean); the Collapse-forward
  action's fallback to change-address semantics; the Reserved-IP `reassign`
  round trip.
- **E2E** (`atlas/tests/e2e/use_cases/virtual_machine_migration.py`): real
  servers; full phase progression, one scenario per address scheme:
  - **Change-address (Self-Managed fallback):** assert the new `/128` is in the
    target's range, `server` flipped, Subdomains re-pointed and the proxy synced.
  - **Keep-address, range-move (Scaleway):** assert `ipv6_address`
    **unchanged**, the block tunnel forwards (reach the VM's `/128` from
    off-host through the source while the FIP is still on the source), the
    `/64` moves once the source drains and the tunnel tears down, and `server`
    flipped.
  - **Keep-address, permanent-forward (DigitalOcean):** assert `ipv6_address`
    **unchanged**; reach the VM's `/128` from off-host through the source's
    proxy-NDP + tunnel indefinitely (poll well past what would be a Scaleway
    drain window, to prove there is no auto-teardown); assert
    `forward_active`/`tunnel_status` stay `1`/`Forwarding` with no scheduler
    action collapsing them; then explicitly invoke Collapse-forward and assert
    it falls through to change-address semantics (new `/128`, Subdomains
    re-pointed, tunnel and proxy-NDP gone).
  - **All three:** source LVs/dir gone, SSH host keys **unchanged** across the
    move, an attached Reserved IP preserved.

This becomes a new row in [README § Operator use cases](./README.md) once built:
`Virtual Machine → Migrate`.
