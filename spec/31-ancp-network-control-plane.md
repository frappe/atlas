# Distributed network control plane (ANCP)

## Abstract

Atlas previously relied on a centralized controller to orchestrate the networking
state of every compute host. The controller maintained global cluster knowledge,
computed WireGuard routing state, and remotely applied configuration changes
whenever VM ownership changed via `reconcile_host_mesh()`.

ANCP replaces that centralized controller with a decentralized control plane.
Every compute host runs an identical networking daemon (`atlas-networkd`) that
collaboratively maintains cluster membership and VM ownership using epidemic
(gossip-based) dissemination and periodic anti-entropy synchronization.

The Atlas controller is completely removed from the **networking** control plane.

Networking is now an eventually-consistent distributed system whose only
responsibility is determining which compute host currently owns a private IP
address.

## 1. Motivation

Before ANCP, Atlas networking would work approximately as follows:

VM migrates → Atlas detects migration → Controller discovers cluster state →
Controller computes WireGuard configuration → Controller SSHes into affected
hosts → Hosts reload WireGuard.

The controller owned multiple unrelated responsibilities: cluster
membership, VM ownership, routing computation, route distribution, remote
execution, and reconciliation. Every networking change required the controller
to recompute global state. As the number of compute hosts increased, the
controller became responsible for maintaining an increasingly large amount of
distributed state.

ANCP eliminates the controller from networking entirely. The previous
controller-side logic lived in `atlas/atlas/host_mesh.py`
(`reconcile_host_mesh`, `sequenced_migration_cutover`, `enqueue_reconcile_host_mesh`)
plus the Frappe `Server`/`VirtualMachine` reads that fed it. ANCP replaced
that entire module: ownership reads become local scans, the controller push
becomes gossip, the converging reconcile becomes anti-entropy, and the
sequenced cutover becomes the eventual-consistency rule in §16.

## 2. Goals

- Remove the centralized networking controller.
- Remove SSH-based networking reconciliation.
- Eliminate globally computed routing tables.
- Decouple networking from VM lifecycle events.
- Keep distributed state minimal.
- Maintain eventual consistency.
- Avoid permanent leaders.
- Avoid N×N broadcast traffic.
- Recover automatically from transient failures.

## 3. Non Goals

This protocol intentionally does not manage: VM scheduling, VM migration, VM
creation, VM deletion, snapshot restore, failover policy, or tenant placement.
These belong to the virtualization layer. Networking only consumes the result.

## 4. Design Philosophy

Networking should not understand virtualization. Instead of asking "a migration
happened," networking asks only "which host currently owns this private IP?"
Everything else is irrelevant. Ownership is the only information networking
requires.

## 5. Architecture

Every compute host runs exactly one networking daemon.

```
Firecracker
   │
Local Ownership Discovery   (atlas-networkd scans non-terminated VMs on this host)
   │
   ▼
atlas-networkd
   │
Atlas Network Control Protocol (gossip + anti-entropy inside wg-mesh)
   │
   ▼
WireGuard (wg-mesh) + nftables (per-VM veth, owned by vm-network-up.py)
   │
Packet Forwarding
```

Each layer owns exactly one responsibility. The customer-gateway client `/128`
folding is preserved as just another "locally owned /128" on the gateway host —
see §11.

## 6. Responsibilities

`atlas-networkd` is responsible for:

- Discovering local ownership (periodic scan of resident VMs + active VPC client
  peers whose gateway VM runs on this host).
- Maintaining Membership Records.
- Maintaining Ownership Records.
- Failure detection (SWIM probes).
- Disseminating updates (gossip + anti-entropy).
- Programming `wg-mesh` (the WireGuard device in the host root netns).

`atlas-networkd` is **not** responsible for:

- Per-VM nftables isolation / anti-spoof at the veth. That stays in
  `scripts/lib/atlas/private_network.py` driven by `vm-network-up.py` /
  `vm-network-down.py` from the VM's Frappe row. The control plane never touches
  the per-VM veth ruleset.
- VM scheduling, migration, creation, deletion, snapshot, restore, failover.
- The public IPv6 plane (`allocate_ipv6`, proxy-NDP) and NAT44 egress
  (`egress_nat44`/`IPV4_EGRESS_SUPERNET`). Those are unchanged and unrelated to
  the private `fdaa::/16` plane ANCP owns.
- The management tunnel (`spec/21`) and the customer gateway's in-guest `wg0`
  (`spec/25` Phase 5). Both share UDP 51820 but are distinct planes.

> **Convention deviation, deliberate.** `llm/Taste.md` rules "one operation =
> one script = one Task row" and "no agent runs on the server." `atlas-networkd`
> is a long-running daemon, not a Frappe Task script. It is an explicit
> exception, the same shape `host-mesh.service` already takes: a systemd unit on
> the host, not a controller-initiated task. The VM-lifecycle shell scripts keep
> the existing Task convention; the daemon owns only the long-lived gossip +
> wg-mesh state.

## 7. Distributed State

Only two distributed objects exist. Everything else is derived.

This section specifies the concrete wire shape of both records, the generation
semantics, and the conflict-detection rule. It resolves **Issue A** (keys are
self-generated, not derived) and **Issue C** (cross-origin generations are
never compared).

### 7.1 Membership Record

A Membership Record describes one compute host. It is **mutable**: a host may
re-issue its own Membership Record at a higher Generation to change its
endpoint, rotate its key, or move between membership states. Only the **origin
host** may mutate its own Membership Record (cross-origin forwarding of a
Membership update is forbidden — see §19).

```
MembershipRecord {
  host_id        : HostID            // 128-bit UUID (the Frappe Server name at provision)
  kind           : enum { member, leaving }   // leaving = graceful-shutdown notice (§14)
  state          : enum { alive, suspect, dead }  // alive/suspect local; dead gc'd (§14)
  endpoint       : Endpoint          // public IPv6 + WG port, e.g. "[2001:db9::7]:51820"
  wg_public_key  : PubKey            // 32-byte Curve25519 raw, base64
  mesh_address   : IPv6             // fdaa:0:0:<idx>::1 — the host's bus /128 (infra /48)
  generation     : uint64            // monotonic per-origin (this host_id), persisted
  // last_seen is NOT replicated — each receiver stamps its LOCAL wall clock
  // on receipt; §14 uses absence-of-heartbeat, never compared clocks.
}
```

Field semantics:

- **`host_id`** is the stable identity supplied at provision (today the `Server`
  row's UUID name). It never changes for the life of the host, even across
  reprovisions that get a new key — see key rotation below.
- **`wg_public_key`** is **derived deterministically** from the Server UUID via
  `derive_host_wireguard_keypair` (HKDF-expand from
  `HKDF("atlas-private-network", HostID)`, a real Curve25519 base-point multiply
  verified byte-for-byte against `wg pubkey`). The private key is computed at
  provision time on the controller and written to the host at bootstrap
  (`/etc/atlas-networkd/wg-private-key`, `0600`). The daemon reads it via
  `ensure_keypair`'s idempotency check (falling back to `wg genkey` if absent
  for dev/manual setups).
  - **Deviation from original design (Issue A deferred).** The original ANCP
    design specified self-generated keys (so the derivation seed — the public
    HostID — could not leak the private key if one host was compromised). In
    practice the deployment keeps the controller as the trusted bootstrap root
    (key material moves over the root-SSH layer, never over gossip), and the
    controller-derivation model gives the critical operational property that a
    re-bootstrap re-derives the same identity with zero peer churn. A future
    phase may move to self-generated keys with an out-of-band key registration
    step; until then the §10.3 late-arriving-Generation guard handles a
    re-provisioned host (fresh keypair, higher Generation) as ordinary key
    rotation.
  - `mesh_address` is **still derived** from `host_id` via
    `derive_host_mesh_address` (HKDF, 16-bit host index, infra /48). It is an
    *address*, not a secret; the derivation gives collision-free numbering with
    no allocator and no extra state. It is carried in the Membership Record both
    for convenience and for collision detection (two hosts advertising the same
    `mesh_address` is a birthday collision at ~√(2¹⁶) ≈ 320 hosts — well above
    the spec/25 ~100–200-host ceiling today; flagged as a scaling limit in §20).
- **`endpoint`** is the public IPv6 the WireGuard peer dials. Matches the
  existing `Server.ipv6_address` role; reissuing the record with a new endpoint
  is how a host picks up a new public IP (endpoint change = generation bump).
- **`generation`** is a 64-bit unsigned integer, **monotonic per origin
  (host_id)**, persisted to disk. On a higher generation from the same origin,
  the receiver replaces the previous record wholesale. A lower generation from
  the same origin is dropped (the §10 rule). **Cross-origin generations are
  never compared** (Issue C; see §7.2). Generation starts at 1 on first boot and
  increments by 1 on every membership-affecting change (join, key rotation,
  endpoint change, state transition). It is loaded from disk on restart, so a
  crash does not reset it — that would let a restart re-overwrite a peer's
  newer view with a stale gen-1 record.

### 7.2 Ownership Record

An Ownership Record maps one private `/128` to its current owner host. The
effective ownership table is the **union of the latest advertisement per
origin**: each origin advertises a *full set* of the `/128`s it currently owns,
and a receiver updates that origin's entry in the table on a higher Generation
from that origin. A `/128` may appear in **at most one** origin's active set;
two origins advertising it is the **conflict** of §18.

This resolves **Issue C**: the original draft keyed duplicate suppression on
`(Origin, Generation)` and left the cross-origin rule implicit. An
"highest-generation-wins across origins" reader would silently elect an owner
on a stolen higher generation. We forbid cross-origin comparison entirely.

```
OwnershipRecord {               // a single claim
  owner_host   : HostID
  private_ip   : IPv6            // the fdaa:: /128
  origin       : HostID          // == owner_host — see invariant below
  generation   : uint64          // monotonic per owner_host, persisted
}

OwnershipAdvertisement {        // the full per-origin bundle on the wire
  origin       : HostID
  generation   : uint64          // bumped on every local change to the set
  owned        : [IPv6]          // the full set — never a delta
}
```

Invariants and rules:

- **`origin == owner_host` always.** A host advertises ownership only of /128s
  it locally observes (resident VMs + VPC clients on its gateway VM). It never
  forwards or vouches for another host's ownership; cross-origin forwarding of
  Ownership updates is forbidden (§19).
- **The advertisement is a full set, never a delta.** A receiver replaces that
  origin's row in the ownership table with the freshly-advertised set on a
  higher generation, and re-derives the effective table
  (`union_of_latest_per_origin`). Removing a /128 is a later advertisement with
  a smaller set, at a higher generation — no negative advertisements.
- **No cross-origin generation comparison.** Generations only compete within an
  origin, where monotonicity is well-defined (single writer).
- **The effective table is recomputed, not stored**, on every applied update and
  on every anti-entropy fill.
- Persistence: the **advertisement stream** (latest per origin) is persisted so
  a crash-restart recovers without waiting for anti-entropy; the effective table
  is derivable and need not be persisted separately.

**Why a full-set advertisement and not a per-/128 log.** A per-/128 log would
require per-key vector clocks or a tombstone GC; the full-set model needs only a
per-origin monotonic counter, mirrors what `host_mesh._residents_by_host` already
computes (a host -> list of /128s), and keeps the conflict surface flat (§18 is
"duplicate in the union," not "two concurrent puts on the same key"). The set
size is bounded by the per-host VM count (today ≤ a few hundred per host); the
advertisement is small (a few KB) and compresses well in the anti-entropy path.

### 7.3 Conflict detection (the §18 invariant made operational)

A **conflict** is any `/128` present in two or more origins' latest
advertisements simultaneously. The protocol:

1. **Never elects.** Networking does not pick a winner by generation, host_id,
   or any other field.
2. **Reports loudly.** `atlas-networkd` logs at `ERROR`, surfaces a counter in
   `/var/lib/atlas-networkd/status.json`, and (operator hook) publishes an event.
3. **Drops, then route-pins.** Until the conflict clears, the /128 is **dropped**
   from the local `wg-mesh` `AllowedIPs` entirely — no peer advertises it.
   Rationale: an arbitrary pick would risk silent misdelivery; a drop turns the
   conflict into a visible blackhole that self-heals the instant the
   virtualisation layer corrects the double-ownership (which it must — the §18
   invariant holds everywhere except transiently during a badly-sequenced
   migration; §16 specifies the soft-sequencing that prevents the common case).
4. The conflict is resolved **by the virtualisation layer** (the migration
   controller stops advertising on the source), at which point one origin's set
   drops the /128, the next advertisement propagates, and the conflict clears.

This is a hard rule, not a heuristic: there is no quorum, no lease, no
last-writer-wins, no lowest-host-id tiebreak. The protocol's only commitment on
a conflict is "do not forward traffic for this address until one owner remains."

## 8. Bootstrapping

(The join sequence itself is §9.)

When Atlas provisions a new compute host, it supplies static configuration via
the existing bootstrap path via `Server._write_ancp_bootstrap_state()`:

- `HostID` (the Server UUID).
- `WireGuard keypair` — the controller derives the keypair deterministically
  from the HostID via `derive_host_wireguard_keypair` and writes both
  `/etc/atlas-networkd/wg-private-key` (0600, via `sudo install -m 0600`) and
  `/etc/atlas-networkd/wg-public-key` (0644) to the host. The daemon reads them
  on startup via `keys.ensure_keypair()`, which is idempotent (falls back to
  `wg genkey` if absent for dev/manual setups). Note: the original design
  specified first-boot self-generation (Issue A); see §7.1 for the deviation
  rationale.
- `identity.json` — the host's `{host_id, endpoint, mesh_address}` record.
- `Initial Membership Seed` (`seed.json`) — a list of `(host_id, endpoint,
  wg_public_key, mesh_address, generation=1)` records for every other Active
  Server at bootstrap time. These seed entries are **trust-on-first-use**: the
  bootstrap installs them as initial Membership Records; thereafter they are
  updated only by signed, higher-generation advertisements from their respective
  origins (§19).

`mesh_address` is derived (HKDF) at provision time and stamped onto the seed
and identity — it is an address, not a secret.

No controller participates in the networking plane after provisioning. §9
specifies the join sequence diagram.

## 9. Joining the Cluster

After startup (§8 first-boot, or §14.5 warm restart), the new node begins
speaking the normal protocol. There is **no dedicated join protocol** — joining
is simply the creation of another Membership Record, propagated by the same
gossip and anti-entropy channels that carry every other update.

### 9.1 Cold-join sequence (first boot)

```
New node N                         Seed S1, S2, S3 (from bootstrap seed file)
   │
   1. bring_up_mesh()         # device up, mesh_address/128 set, listen-port, peers empty
   2. read seed file -> initial Membership Records (alive, gen=seed-gen from provision)
   3. install seeds as wg-mesh peers (atomic syncconf, §16.4)
   4. self-advertise:  N --MembershipAdvertisement(gen=1)--> S1, S2, S3   (unicast over wg-mesh)
   │
   └──────────────────────────────────────────────────────────────────────┐
                                                                          ▼
   Each seed Si:                                                           │
     - validates: Origin=N, gen=1, N not yet known, wg handshake OK       │
     - installs N's Membership Record (alive, gen=1)                       │
     - adds N as a wg-mesh peer (atomic syncconf)                          │
     - replies with Si's OWN Membership Advertisement (gen=Si.gen)         │
        AND a bundle of the OTHER members' latest Membership Records       │
        (state transfer — fast-paths the antientropy fill)                 │
     - gossips N's Membership Advertisement in the next gossip round       │
                                                                          │
   N receives the bundle:                                                  │
     - installs every enclosed Membership Record at its latest gen         │
     - recomputes WgDesired, atomic syncconf                              │
     - now has the full cluster peer set                                   │
     ◄────────────────────────────────────────────────────────────────────┘
   5. start periodic gossip + probe + anti-entropy loops (§10, §14, §15)
```

The bundle-reply in step 4 is **state transfer over the join unicast**, the same
optimization Serf/memberlist use: it folds what would otherwise be a second
anti-entropy round into the join acknowledgement. It is **optional** — a seed
may simply reply with its own record and let periodic anti-entropy fill the
rest within one round (§15). The bundle is a latency optimization, not a
correctness requirement.

### 9.2 Seed trust model (Issue A closeout)

The seeds are **trust-on-first-use** installed at provision time; the
bootstrap path signs the seed file with the operator's provision key (outside
ANCP) so a host can't be given a poisoned seed list by a network attacker at
first boot. After install:

- A seed's `wg_public_key` is fixed unless the seed origin later advertises a
  key rotation at a higher Generation (§10). The handshake authenticates the
  peer; the Membership Record authenticates the *identity* the peer is
  claiming.
- Cross-origin forwarding of Membership updates is forbidden (§19), so a host
  can only forge its own Membership Record — never anyone else's.
- A newcomer with no live seeds (all partitioned / dead) blocks on
  `join_retry_interval` (default 1 s) re-trying each seed until one answers or
  itself times out; the host comes up peer-empty and waits. This is the same
  posture as today's `bring_up_mesh` waiting for the first reconcile.

### 9.3 Warm restart (after §14.5 crash recovery)

Load persisted tables and Generation counter, skip steps 1–3 (device already
up, tables already loaded), send a fast-refute `alive` Membership
Advertisement (Generation = persisted+1) to every seed. Peers re-accept (or,
if they had reached `dead`, treat it as a normal join at the higher Generation)
within one round.

## 10. Membership Dissemination

Membership information — joins, endpoint changes, key rotations, state
transitions — disseminates over the same gossip channel as everything else.
Each Membership Record carries a per-origin monotonic Generation (§7.1). The
SWIM-with-Lifeguard state machine (§14.1) drives the observer-local
`alive/suspect/dead` ladder; this section specifies how the records move.

### 10.1 What triggers a Membership Advertisement

The origin bumps its own Generation (persisted, +1) and emits a Membership
Advertisement on:

- **First boot** (join) — Generation 1.
- **Endpoint change** — the host's public IPv6 moved (e.g. a Droplet rebuild
  at a new address).
- **Key rotation** — the host re-keyed its WireGuard keypair. Old peers see a
  Membership Record with a new `wg_public_key` at a higher Generation; the
  §13.1 forwarding rule applies (drop the previous peer's pubkey, install the
  new). The handshake re-keys transparently.
- **State transition to `leaving`** (graceful shutdown, §14.4).
- **Fast-refute** after partition recovery (§14.5).

Generation is loaded from disk on restart so a crash-restart produces
Generation = persisted+1, never reset to 1 (that would let a stale lower-gen
record overwrite a peer's newer view).

### 10.2 Gossip fan-out (cross-references §13)

Dissemination mechanics — peer selection, round cadence, forwarding, duplicate
suppression — are specified once in §13 and apply uniformly to Membership and
Ownership. The §14 probe piggybacks Membership state advertisements on the
same gossip rounds, exactly as SWIM piggybacks membership on probes.

### 10.3 Late-arriving Generation guard

If a host receives a Membership Record from origin X at Generation 12 after
already having processed one at Generation 15 from the same origin X, the
update is discarded (the §10 rule from the original draft, now in §13.2's
duplicate-shaping). A lower-Generation record never overwrites — this is the
replay-protection primitive; no clocks, no signatures, just per-origin
monotonicity.

## 11. Ownership Discovery

Networking never listens for migration events. Instead, `atlas-networkd`
periodically scans local ownership — discovering which private /128s belong to
locally-running VMs via the local-ownership cache maintained by the
VM-lifecycle scripts (see §11.3).

### 11.1 The local scan

```
owned_local(N) =
    { derive_private_address(tenant, vm)                  # every non-terminated VM on N
        for each VirtualMachine row VM on N
        where VM.status ∉ {Terminated, Draft}
        and VM.tenant is not None }
  ∪ { derive_client_address(tenant, peer)                # every Active VPC client
        for each VPN Peer peer whose gateway VM runs on N
        where peer.status == Active }
```

The addresses are the same pure HKDF derivations from
`atlas/atlas/networking.py` (`derive_private_address`,
`derive_client_address`). ANCP does not re-derive them — it reads the
`/etc/atlas-networkd/local-ownership.json` cache that the VM-lifecycle scripts
maintain (see §11.3) so it has the same source of truth without polling Frappe.

### 11.2 Scan cadence (`ownership_scan_interval`, default 2 s)

Every `ownership_scan_interval`, `atlas-networkd` re-reads the local-ownership
cache and compares to its last advertised set:

- **Unchanged** → emit nothing. The local Generation stays; the existing
  advertisement continues to ride outgoing gossip.
- **Changed** → bump Generation, publish a fresh Ownership Advertisement
  (full per-origin set, §7.2) in the next gossip round. The change is debounced
  by `apply_debounce` (200 ms) so a sequence of two lifecycle events in a row
  (e.g. a stop followed by a start of a different VM) produces one advertise,
  not two.

### 11.3 Where the local-ownership cache comes from

Two producers write `/etc/atlas-networkd/local-ownership.json`:

1. **`vm-network-up.py` / `vm-network-down.py`** (the existing per-VM scripts
   that already install the veth nft rules) append/remove their VM's private
   /128. This is the same hook point the scripts already have — they already
   read `PRIVATE_ADDRESS` / `TENANT_PREFIX` from the provision env; writing the
   cache entry on success is one extra line.
2. **`atlas-networkd` itself on startup** reads the cache; a missing or empty
   cache produces an empty set (no locally-owned /128s), and the daemon waits
   for the VM-lifecycle scripts to write entries. There is no Frappe DB
   fallback — the cache is the only source of truth, keeping the networking
   layer decoupled from virtualization (§4).

Writing the cache is by the VM-lifecycle scripts (atomic `O_TMPFILE` + `rename`)
so `atlas-networkd` never reads a half-written file. The cache is the seam
between the VM-layer (touch a file) and the network-layer (read a file); it
keeps the §4 decoupling ("networking should not understand virtualization")
intact — `atlas-networkd` reads an address list, nothing more.

### 11.4 What the daemon never determines

It does not determine *why* a /128 disappeared from the local scan (terminated?
migrated? veth torn down for a stop?). It only publishes the new set. Today's
"keep-address migration" path stays inside the virtualisation layer — the VM
row's `server` field moves, the host-local scan reflects it, the advertisement
changes, ANCP never knows a migration happened.

## 12. Ownership Advertisements

Whenever local ownership changes (§11.2), `atlas-networkd` publishes the
**current ownership state** as a full per-origin bundle (§7.2). The wire shape
is a host → a list of /128s, so the §16.2 `WgDesired` derivation is a direct
union over origins, not a per-/128 merge.

### 12.1 Trigger

| Event                                      | Advertisement?          |
| ---                                        | ---                      |
| Local scan changed (VM provision/terminate, VPC peer enroll/revoke) | yes, Generation+1 |
| Host started up (warm restart)             | yes, Generation = persisted+1 (refute-shaped) |
| Periodic refresh (`advertisement_refresh_interval`, default 60 s) | yes, no Generation bump (same set, generation unchanged — just reminders for peers who missed an update) |
| Local scan unchanged between scans         | none                     |

The periodic refresh is the "carrier wave" that re-propagates the host's
current ownership for any peer that missed the original update — it's not a
re-advertise with a higher generation, just re-attachment of the latest
advertisement to outgoing gossip messages. This is the same idiom Serf uses for
its "recently changed" set that piggybacks on every gossip round; the
`advertisement_refresh_interval` bounds how long a host re-sends before
assuming anti-entropy has carried the update.

### 12.2 No lifecycle events on the wire

Messages on the wire never carry `migrated`, `created`, `deleted`, `restored`.
Only ownership — exactly the original draft's §12 invariant. Decoupling
remains absolute.

## 13. Gossip Dissemination

Specifies the mechanics that §10 and §12 both use: peer selection, fan-out,
forwarding, duplicate suppression, and the seen-cache. Resolves the
**Issue C** carryover (cross-origin generations are never compared in the
forwarding layer).

### 13.1 Gossip rounds

Every node maintains the complete membership table (the alive + suspect ones;
dead ones are removed by §14.6 after `dead_grace`). During each
`gossip_interval` (default 200 ms), a node selects **`gossip_fanout` peers
(default 3)** at random from the membership table and sends each a `Gossip`
message:

```
Gossip {
  sender           : HostID                    // == wg-authenticated peer
  piggyback : [                                    # all optional
    MembershipRecord | OwnershipAdvertisement,
    ...
  ]
  # plus a compact-summary tail for anti-entropy (§15), piggybacked to amortize
}
```

Peer selection is **health-aware** (SWIM Lifeguard's "Health-aware probing"
contribution): peers recently probed, peers in `suspect` state, and peers
recently failed are underweighted so a flapping peer doesn't dominate the
round. The selection excludes self and excludes peers that have been `dead`
for more than `dead_grace`.

### 13.2 Forwarding

When a node receives a `Gossip` carrying records it has not yet applied:

1. For each Membership Record `M` and Ownership Advertisement `O`:
   - Compute `(origin, kind, generation)` — the dedupe key.
   - Look up the seen-cache (§13.3). If hit → drop silently.
   - Otherwise: apply the rule below and mark the key seen.
2. For a **Membership Record** (origin = `host_id`):
   - If `M.generation > applied_membership[host_id].generation`: replace
     wholesale (the §10.3 rule).
   - Else (stale): drop.
3. For an **Ownership Advertisement** (origin = `owner_host`):
   - If `O.generation > applied_ownership[origin].generation`: replace that
     origin's full-set entry wholesale.
   - Else: drop.
   - **The forwarding layer never compares generations across origins.**
     (Issue C'est close-out here too: any cross-origin comparison would
     silently elect; we forbid it by construction.)
4. After applying changes, atomically re-derive the effective ownership table
   (§7.2 union-over-origins) and schedule a debounced §16.4 apply.
5. **Forward** the newly-applied records in the next `gossip_interval` round
   (sample: forward each freshly-applied update to `gossip_fanout` freshly
   selected peers; bound the per-round forwarding budget by
   `gossip_forward_budget`, default 16 records, so a single update doesn't
   starve a burst).

Eventually every node observes the update. Convergence is O(log N) gossip
rounds under the standard epidemic assumption (each round reaches ≥ k random
peers); §15 anti-entropy is the correctness backstop when gossip drops a
delivery.

### 13.3 Duplicate suppression

Uniquely identified by `(origin, kind, generation)` where `kind ∈
{membership, ownership}`. Each node keeps a **bounded LRU seen-cache** sized
at `seen_cache_size` (default 10 000 entries) — large enough to cover the
longest partition the cluster is expected to heal from without re-applying an
already-applied record. On hit → drop silently (no re-forward). On miss → apply
(§13.2) and mark seen.

The cache is persisted across restarts so a crash-restart doesn't replay
already-applied records onto the wire. (For very long partitions exceeding the
cache size, the worst case is a few harmless re-applies of stale-but-still-
newer-than-local records — the generation check at §13.2 step 2/3 demotes them
anyway. The cache is an optimization, not a correctness requirement; the
per-origin Generation check is the correctness primitive.)

### 13.4 Replay protection summary

- **Within an origin** — monotonic Generation + persisted counter + LRU drop
  rejects any replay of a Generation the host has already seen (or a stale
  lower-generation) silently.
- **Across origins** — no comparison is ever made; a record can only advance
  its own origin's state.
- **WireGuard private key replay** — Issue A makes the key self-generated and
  the public half ride the Membership Record; a stale old key advertized at a
  lower Generation is rejected by the §10.3 guard (its recorded Generation for
  that origin is higher).
- **Boot forged seed** — §9.2 (operator-signed seed file) is the only
  unauthenticated entry point; afterwards, all updates come authenticated over
  wg-mesh from the origin whose identity matches the record.

## 14. Failure Detection & Membership Lifecycle

Specifies the SWIM-with-Lifeguard state machine and timers. Resolves **Issue D**
(false eviction).

### 14.1 State machine

Each Membership Record carries a `state` field. Transitions are driven by the
probe protocol and by self-advertisements. States:

- **`alive`** — normal operation.
- **`suspect`** — a probe failed (direct + indirect). The host is suspected
  unreachable, **not** declared dead. The origin host may **refute**.
- **`dead`** — `suspect` held past `suspect_timeout` without a refute, or the
  origin self-advertised `kind=leaving`. Terminal: kept for `dead_grace` then
  garbage-collected.

```
                       refuted (alive msg with gen > our last-seen gen)
            ┌──────────────────────────────────────────────────────────┐
            ▼                                                          │
         alive  ──────(probe fail: no ack by indirect_timeout)────►  suspect
           ▲                                                            │
           │                                                            │ (suspect_timeout elapses,
           │                                                             │  no refute received)
           │                                                            ▼
           └─────────────────────────────────────────────────────  dead
                       (no transition out of dead except GC after dead_grace)
```

State changes are Membership Record updates with a higher Generation from the
origin (`suspect`/`dead` are stamped by the observer — see below). The
origin also raises its own Generation when it sends a `refute` (alive)
advertisement, so the receiver's monotonic check accepts it.

> **Quirk baked in.** In SWIM, the `alive`/`suspect`/`dead` field is in the
> view of a particular observer, not an objective global fact; two hosts can
> disagree. We carry `state` on the **origin's** Membership Record for the
> `leaving` case and on the **observer's** local copy for the
> `alive`→`suspect`→`dead` ladder — they are different fields on different
> rows. The wire Membership Record fields in §7.1 (`kind`, `state`) are the
> origin's view; the observer's ladder lives only in local state at
> `/var/lib/atlas-networkd/membership.json`. Anti-entropy carries only the
> origin's fields; the ladder is recomputed locally.

### 14.2 Probe protocol

Each `probe_interval`, a node selects `probe_peers` random members from its
membership table (excluding self and recently-probed — see health-aware
selection below) and sends each a `Ping`. The `Ping` piggybacks on the gossip
round.

1. A peer that receives a `Ping` replies `Ack` immediately (inside wg-mesh).
2. If the prober receives `Ack` within `probe_timeout` → peer stays `alive`.
3. If not, the prober selects `indirect Relay_peers` other members (default 3)
   and sends each an `IndirectPing(target=peer)`. Each relay forwards a `Ping`
   to the target and returns the `Ack` (or timeout) to the prober.
4. If the prober receives a relayed `Ack` within `indirect_timeout` → `alive`.
5. Otherwise → `suspect`. Bump the observer's local view of that host's state
   and (optionally, to make suspicion visible across the cluster faster) gossip
   a `Suspect(host_id, generation)` notice. Importantly, the original Member
   Record's wire generation is **not** mutated by a non-origin observer — see
   §19 cross-origin rule.

The origin refutes by its normal gossip path emitting a fresh `alive`
Membership Advertisement with a `generation` higher than the latest the
suspicious observer has recorded from it. A refuting host that was partitioned
doesn't need a special message — its next periodic advertisement qualifies, but
we add an explicit **fast-refute** on first heartbeat-after-recovery so the
suspicion lifts within one round.

### 14.3 Timers

Concrete defaults (all operator-tunable via `/etc/atlas-networkd/ancp.toml`):

| Timer                | Default | Meaning |
| ---                  | ---     | --- |
| `probe_interval`     | 1 s     | Probe round cadence. |
| `probe_timeout`      | 500 ms  | Direct ACK wait. |
| `indirect_timeout`   | 2 s     | Indirect (relayed) ACK wait. |
| `probe_peers`        | 3       | Members pinged per probe round. |
| `indirect_relays`    | 3       | Members asked to relay per failed probe. |
| `suspect_timeout`    | 10 s    | Suspect → dead if no refute. **This is the partition knob**: set comfortably longer than the longest partition you want to tolerate without orphaning the partitioned side's VMs (e.g. 60 s on a flaky-WAN fleet). |
| `dead_grace`        | 30 s    | dead Record retention before GC. |
| `ownership_grace`   | 60 s    | After an origin goes `dead`, its Ownership Records survive this long before being dropped — strictly longer than `suspect_timeout + dead_grace` so a host that refutes late (partition just long enough to hit `suspect` then recovers) doesn't lose its routes mid-refute. |
| `leaving_grace`     | 2 s     | On a `kind=leaving` Membership Advertisement from the origin, peers fast-path alive → dead skipping `suspect` after this short wait. |
| `gossip_interval`   | 200 ms  | Gossip fan-out round (§13). |
| `anti_entropy_interval` | 1 s  | Anti-entropy peer pull (§15). |

Worst-case failure detection ≈ `probe_timeout + indirect_timeout + suspect_timeout
+ dead_grace` ≈ 42 s. With `suspect_timeout` raised to 60 s for partition-prone
WANs, ≈ 92 s. This is **intentional** — false eviction is a worse failure mode
than slow detection (it takes a host's VMs cluster-wide offline even though they
are healthy). The job is to avoid wrongful eviction, not to detect crashes in
subsecond time.

### 14.4 Graceful shutdown

On `SIGTERM` (or systemd `ExecStop`), `atlas-networkd`:

1. Sends a Membership Advertisement with `kind=leaving` (Generation bump).
2. Drains in-flight gossip (200 ms debounce window).
3. Optionally asserts `wg-mesh` stays up (leave the interface and peers intact)
   so a fast restart (a `systemctl restart`) does not look like a partition to
   peers — peers will fast-path to `dead` after `leaving_grace` anyway, but a
   sub-2 s restart that re-advertises `alive` refutes before the grace elapses.
   Recommendation: keep `wg-mesh` up on shutdown; only `ip link del` if the
   operator explicitly decommissions the host.
4. Persists the Membership/Ownership tables.
5. Exits.

### 14.5 Crash recovery (local)

A `dead_grace`-long outage already tears the host down on peers; on restart:

1. Load persisted `(generation, tables)` from `/var/lib/atlas-networkd/`.
2. Re-derive local ownership from scan (§11) — the /128 set is the source of
   truth; persisted ownership is only a recovery cache.
3. Bring `wg-mesh` up peer-empty (device, MTU 1420, own mesh /128, route
   `fdaa::/16 dev wg-mesh`, **set the private key last**).
4. Send a fast-refute `alive` Membership Advertisement (Generation =
   persisted+1) to every seed.
5. Anti-entropy reconciles the rest within one round.

If the outage was short enough that peers had not yet reached `suspect`
(< `probe_timeout + indirect_timeout` typically), no one noticed. If the outage
exceeded `suspect_timeout` but not `suspect_timeout + dead_grace + ownership_grace`,
peers mark `dead` and tear down the wg peer, but the persisted Ownership Records
on peers are still within grace and are resurrected by the fast-refute.
Rejoining after a full `dead_grace + ownership_grace` is just a normal re-join
(§9) — the host's Membership Record returns at a higher Generation and re-seeds
its ownership via periodic advertisement.

### 14.6 Garbage collection

- `dead` Membership Records are removed after `dead_grace`. Once removed, the
  host is no longer a gossip target, no longer an indirect-relay candidate, and
  not part of anti-entropy summaries. The origin may rejoin later by issuing a
  Membership Record at a Generation higher than any record it had previously
  persisted (its own counter on disk) — peers accept it as a normal join at §9.
- Ownership Records whose origin is `dead` are dropped after `ownership_grace`.
  A different host that takes over one of those /128s (failover) is just
  another origin advertising ownership — the new advertisement is the
  cross-origin concurrent-client case of §7.3 until the old origin's set
  disappears, at which point the conflict clears and the new owner routes.

### 14.7 Network partitions

- `suspect_timeout` is the knob. A partition shorter than `suspect_timeout` is
  invisible — both sides keep each other `alive`, anti-entropy is paused, and
  on heal the generation vectors reconcile normally.
- A partition longer than `suspect_timeout` but shorter than `suspect_timeout +
  ownership_grace` makes each side mark the other `suspect` then `dead`; once
  `dead`, each side routes to its own owners (cross-partition hosts are
  unreachable and packets drop at the tunnel). On heal: fast-refute reverts
  membership; anti-entropy reconciles the ownership tables; a /128 that both
  sides advanced to different owners is the §7.3 conflict.

## 15. Anti-Entropy

Gossip gives rapid spread but does not guarantee delivery. ANCP runs periodic
anti-entropy so the cluster converges even after sustained packet loss, a
partition, or a daemon restart with no missed-update propagation.

### 15.1 The generation vector (summary)

Each node maintains a compact summary of the latest generation it has applied
per origin, for both record kinds:

```
GenerationVector {
  membership : {(HostID, generation)}
  ownership  : {(HostID, generation)}       # one entry per origin host
}
```

Size is O(N) (two ints per member). At N=200 hosts it's ~2 KB, trivial to
piggyback on every gossip round (§13.1 mentions the compact-summary tail).

### 15.2 Pull exchange (the canonical anti-entropy shape)

Every `anti_entropy_interval` (default 1 s), a node selects **one random peer**
(not recently chosen — a slow sweep converges faster than a uniform-random one)
and performs a pull:

```
Initiator I                              Peer P
   │ --AntiEntropy.Request{I.summary}--> │
   │                                      │  compute missing = P.records where P.gen > I.gen
   │                                      │  and (optionally, for fast catch-up)
   │                                      │  compute newer-on-I = I.records where I.gen > P.gen
   │ <--AntiEntropy.Response{             │
   │       missing_records : [Membership|Ownership]   # everything I lacks
   │       newer_on_initiator: GenerationVector       # tell I what P also lacks (next-request hint)
   │    }--------------------------------|
   │ apply each missing (§13.2 rules)    │
   │                                     │
   │ (optional reverse pass: I sends P the records P indicated it's missing)
   │ --AntiEntropy.Response{...}------>  │
```

A symmetric push-back (the optional reverse pass) makes a single peer
exchange mutually healing rather than one-directional. Keep it — it's a
one-line additional `Gossip` round and converges partitions in roughly half
the round count.

### 15.3 Merkle acceleration (optional, recommended past ~100 hosts)

At small cluster sizes the naive pull — "send me every record where your gen
is higher than mine" — is fine: the responder sends every out-of-date record
on every exchange, bounded by the number of stale records, and a healthy
cluster sends nothing. Past ~100 hosts, or on a long partition healing, a
**Merkle prefix tree** over `(HostID, generation)` pairs reduces a full-tree
comparison to O(log N) hashed nodes before any records ship:

- Each node builds a binary Merkle tree over its GenerationVector sorted by
  `HostID`. The root hash + per-level subtree hashes ride the
  `AntiEntropy.Request` (16 + ~log₂(N)×32 bytes; ~256 B at N=200).
- The responder compares its tree to the requester's, descending only into
  differing subtrees, and replies with the leaf entries (HostID, generation)
  where the requester is missing or stale — exactly the §15.2 set.
- The responder then sends the corresponding records in the same reply.

The Merkle tree is an **optimization**, not a correctness requirement; naive
pull converges identically, just with more bytes on the wire during catch-up.
Default: enabled when `cluster_size > ANTI_ENTROPY_MERKLE_THRESHOLD` (default
100 hosts), disabled below — the protocol is the same either way.

### 15.4 Convergence guarantee (Demers' result, restated)

If the network is eventually connected — every pair of nodes can reach every
other pair through some relay path eventually — and anti-entropy runs on every
node with positive probability of selecting every peer, then every update
applied at any node reaches every other node in **bounded expected time**: one
anti-entropy round plus a number of gossip rounds logarithmic in N under the
random-fan-out assumption. This is the property §17 makes precise; anti-entropy
is the correctness side of it, gossip the latency side.

### 15.5 Anti-entropy vs. gossip

| | Gossip (§13) | Anti-entropy (§15) |
| --- | --- | --- |
| Cadence | every `gossip_interval` (200 ms) | every `anti_entropy_interval` (1 s) |
| Fan-out | k random peers per round | one peer per round |
| Payload | recently-changed records (piggybacked) | missing records (pull on summary) |
| Best at | low-latency spread of fresh changes | guaranteed catch-up over lossy channels |
| Used for | the hot path | the correctness backstop |

Both channels run on every node; updates can arrive on either; §13.2's apply
rules are identical. A burst of changes flows on gossip in ≤ O(log N) rounds;
a partition's reconciliation rides anti-entropy once the partition heals.

## 16. WireGuard Synchronization

Specifies how `atlas-networkd` keeps `wg-mesh` consistent with the effective
Membership + Ownership tables. Resolves **Issue B** (the non-overlap invariant
must survive eventual consistency) and reasserts the load-bearing apply facts
proven on real hosts (the apply pipeline at `scripts/lib/atlas/networkd/apply.py`).

### 16.1 What the daemon programs

`atlas-networkd` owns the `wg-mesh` device in the **host root netns** (invisible
to every guest netns — unchanged from today). It programs **only**:

- The interface `ListenPort` (51820) and MTU (1420) — set once at bring-up,
  unchanged afterward.
- The host's own infra `mesh_address` /128 (`fdaa:0:0:<idx>::1`) — set once at
  bring-up.
- The `fdaa::/16` route out `wg-mesh` — set once at bring-up.
- The **peer set**, recomputed from the effective tables and re-applied as a
  single atomic `wg syncconf` on every change.

It does **not** program (these stay where they are):

- Per-VM veth nftables rules (`scripts/lib/atlas/private_network.py`).
- Migration tunnel devices / route tables (`mig6-…`, spec/24).
- NAT44 egress (the public v4 plane).
- Customer-gateway in-guest `wg0` (Phase 5, entirely guest-side).

### 16.2 The current peer table (one source of truth, derived)

`atlas-networkd` keeps a single in-memory `WgDesired` (the canonical text
produced by the apply module at `scripts/lib/atlas/networkd/apply.py`),
derived **purely** from the effective tables:

```
WgDesired =
  [Interface] ListenPort=51820
  for each peer p in members (excluding self, state != dead, conflict-free):
    [Peer]
      PublicKey      = p.wg_public_key
      Endpoint       = [p.endpoint.v6]:51820
      AllowedIPs     = sorted(
                          owned(p.host_id)        # /128s whose owner is p
                          ∪ { p.mesh_address }      # the host's bus /128
                        )
      PersistentKeepalive = 25
```

`owned(h)` is the set of /128s whose effective ownership's `owner_host == h`.
The render is byte-canonical (peers sorted by pubkey, /128s sorted) so the
"sync needed?" check is a string compare — the existing `_reconcile_one_host`
pattern.

### 16.3 The non-overlap invariant (Issue B)

**Invariant (must hold at every host, always):** within a single rendered
`WgDesired`, no /128 appears in the `AllowedIPs` of more than one peer.

This is what WireGuard's cryptokey routing actually requires. It does **not**
require cross-host agreement; it requires each host to be internally
unambiguous. The protocol guarantees it by two rules:

1. **Conflict-driven drop.** If a /128 is in two origins' active sets (the §7.3
   conflict), it appears in **no** peer's `AllowedIPs` (dropped, flagged). So a
   conflict can never produce overlap.
2. **Single-winner routing.** If a /128 has exactly one effective owner, it
   appears in exactly one peer's `AllowedIPs`. So the regular case never
   overlaps.

What the protocol **does not** guarantee (and does not need to): that two
different hosts agree on who owns a given /128 *during a migration cutover*.
During the cutover window, one host may route `/128 → source` while another
routes `/128 → target`. Each is internally unambiguous; the worst outcome is a
packet arrives at a host whose local scan says "I no longer own this" — it
drops at the veth (no tap), a **transient blackhole that self-heals in O(log N)
gossip rounds**, not ambiguous delivery. This replaces today's hard two-push
barrier (`sequenced_migration_cutover`) with a soft sequencing expressed as a
property of generation flow:

> **Soft migration sequencing (replaces `sequenced_migration_cutover`).** The
> virtualisation layer's migration controller, *outside* ANCP, causes the
> source host's local scan to drop the /128 before the target host's scan picks
> it up. ANCP makes this soft sequencing produce a clean cutover because:
> - while both origins advertise the /128: §7.3 conflict → drop → blackhole
>   (packets don't misdeliver; they wait).
> - once only the target advertises it: clean unicast routing.
> The migration controller keeps the existing **Server lock** and the
> two-phase (withdraw-from-source, then advertise-on-target) ordering it
> already has; what it loses is the hard *global* barrier that today's
> fleet-wide two-push gives it (the lag is bounded by one anti-entropy round
> rather than a barrier receipt). The tradeoff is intentional — see §17.

### 16.4 The apply pipeline (atomic, whole-table, debounced)

`atlas-networkd` owns a single apply task:

1. Recompute `WgDesired` from the effective tables when any of:
   - a Membership Record is applied;
   - an Ownership Advertisement is applied (which can change the effective table);
   - anti-entropy fills a gap.
2. **Debounce** by `apply_debounce` (default 200 ms). A burst of changes
   becomes one apply. A /128 that hops twice in quick succession produces one
   `syncconf`, never two — important for the Issue B invariant and for not
   churning the WireGuard peer table.
3. If `WgDesired == WgLive`, do nothing.
4. Otherwise, write `WgDesired` to `/run/atlas-networkd/wg-mesh.conf` and apply:

   ```sh
   wg syncconf wg-mesh <(wg-quick strip /run/atlas-networkd/wg-mesh.conf)
   wg set wg-mesh private-key /etc/atlas-networkd/wg-private-key listen-port 51820
   ```

    **Load-bearing ordering** (proven on a real Scaleway host, exercised by the
    `atlas-networkd` apply pipeline): `syncconf` from a config that omits
   `PrivateKey` **clears the interface key**; we therefore `syncconf` first,
   then re-assert the key last. A future implementer who flips this order
   breaks every tunnel.

5. **No incremental `wg set peer … allowed-ips …`** for control-plane
   changes. The only permitted apply shape is the whole-table `syncconf` above.
   Incremental per-peer applies would open a window (peer A added before peer B
   removed) in which the same /128 sits in two peers' `AllowedIPs` and breaks
   the Issue B invariant. This is a hard rule, enforced by code review and by a
   self-test that asserts (post-apply) `wg show wg-mesh dump` has no /128 in
   more than one peer.

### 16.5 First-boot bring-up (mirrors the existing `bring_up_mesh`)

```sh
ip link add dev wg-mesh type wireguard        # if missing
ip link set dev wg-mesh mtu 1420
ip -6 addr replace <mesh_address>/128 dev wg-mesh
wg set wg-mesh private-key /etc/atlas-networkd/wg-private-key listen-port 51820
ip link set dev wg-mesh up
ip -6 route replace fdaa::/16 dev wg-mesh
# peer table populated by the first apply once Membership/Ownership anti-entropy runs
```

This is the same sequence as the original `bring_up_mesh` with the key file
moved to the `atlas-networkd` data directory; the ordering rationale is
preserved.

### 16.6 Customer-gateway client /128s

The VPC client `/128` folding is unchanged in shape: the host running the
gateway VM sees those client /128s as locally owned and advertises them in its
own Ownership advertisement — exactly one more line in §11's local scan. They
ride the same gossip, the same §16.4 atomic apply, the same §7.3 non-overlap.
The control plane does not know "this is a client /128" vs. "this is a VM
/128"; both are just /128s.

## 17. Consistency Model

The protocol provides **eventual consistency**. Made precise here.

### 17.1 What converges

Both record kinds are per-origin monotonic (Membership by §7.1, Ownership by
§7.2). A system where every origin is eventually heard from by every
non-failing node converges to a unique state under §13.2's monotonic-apply
rule: the union of every origin's latest record, deterministic from the
transport's eventual delivery. There is no read quorum, no write quorum, no
leader.

### 17.2 The observable inconsistency window on a single /128

Immediately after an ownership change (origin X stops advertising `/128 = A`,
origin Y starts advertising it):

```
t=0            X drops A from its Ownership Advertisement
               (Generation++, sent on next gossip round)
t ≈ ε          X's first-fan-out peers apply it
t ≈ k·gossip_interval·log(N)   every member has applied X's update
                               (X no longer advertises A)
t ≈ ε'         Y adds A to its Ownership Advertisement
               (Generation++, sent on next gossip round)
t ≈ k·gossip_interval·log(N)   every member has applied Y's update

Sequence: A is owned by X
   → (overlap window: both X and Y advertise A — §7.3 conflict, dropping)
   → A is owned by Y
```

There are three phases:

1. **Pre-change**: A → X. Unicast routing, unambiguous.
2. **Overlap**: both X and Y advertise A at their respective latest Generations.
   The §7.3 conflict rule kicks in at each receiver, A drops out of WgDesired
   (blackhole), the operator sees a transient conflict log line. Duration
   bounded by the time for both parallel gossip waves to converge to each
   observer plus the §16.4 apply debounce — **one gossip diameter + one apply
   window**.
3. **Post-change**: A → Y. Unicast routing, unambiguous.

The worst-case observable inconsistency window on a single /128 move — from
X-advertises-drop to every node has Y-advertised-add — is bounded by:

```
2 · (gossip_diameter · gossip_interval + apply_debounce)
  + 1 · anti_entropy_interval                # if a gossip wave fully drops
```

At the §14.3 defaults (gossip_interval 200 ms, N=200 ⇒ diameter ≈ log₂(200) ≈
8, apply_debounce 200 ms, anti_entropy_interval 1 s), that's roughly
2·(8·200 ms + 200 ms) + 1 s ≈ **4.6 s worst case**. Under normal gossip (no
sustained loss) the observed time is one gossip round ≈ 200 ms in the steady
state.

### 17.3 What the protocol does NOT guarantee

- **No global agreement on routing during the overlap window.** One host routes
  A → X, another routes A → Y; each is internally unambiguous, so
  WireGuard's non-overlap invariant at each host (§16.3) still holds, but
  cross-host routing is inconsistent. A packet routed to the old owner hits a
  host whose local scan says "I no longer own this" and drops at the veth — a
  self-healing blackhole, not misdelivery.
- **No concurrent writer protection across origins.** A /128 with two active
  origins is the §18 conflict; the protocol reports and drops, never elects.
- **No linearizability / read-your-writes.** A host that just advertised an
  ownership change may briefly observe stale routing from a peer that hasn't
  applied it yet.

This is the explicit price of the §2 goals ("no permanent leaders," "no N×N
broadcast," "eventual consistency"). It is appropriate because the data plane
fails safe: misdelivery is impossible (the §16.3 invariant drops on conflict),
the worst outcome is a transient blackhole shorter than typical TCP retry
budgets.

## 18. Conflict Detection (hoisted from §7.3)

A **conflict** is any `/128` present in two or more origins' latest
advertisements simultaneously. The protocol's response, restated here as a
top-level invariant:

1. **Never elects a winner.** Networking does not pick by Generation, HostID,
   or any other field — there is no quorum, no lease, no last-writer-wins, no
   lowest-host-id tiebreak. Conflict resolution is the virtualization layer's
   responsibility (today: the migration controller's Server lock + the soft
   sequencing of §16.3; in the long term: any out-of-band coordination).
2. **Reports loudly.** `atlas-networkd` logs at `ERROR`, surfaces a counter in
   `/var/lib/atlas-networkd/status.json`, and publishes an operator event
   (hook). The conflict is operator-visible so the virtualization layer can
   be notified.
3. **Drops, then pins.** The conflicting /128 is removed from `WgDesired`
   across every host — no peer advertises it. Traffic to that /128 blackholes
   until the conflict clears. This is the only acceptable safe default: an
   arbitrary pick risks silent misdelivery to the wrong tenant.
4. **Self-heals.** The instant the virtualization layer resolves the double-
   ownership (one origin drops the /128 from its next advertisement), the
   conflict clears in one gossip round and unicast routing resumes.

### 18.1 Where conflicts come from

The protocol assumes "one /128 → exactly one owner." A conflict means the
assumption is violated upstream; ANCP never manufactures one. The realistic
sources are:

- A badly-sequenced migration (the virtualization layer moved a VM without
  withdrawing it from the source first) — §16.3's soft sequencing by the
  migration controller is the prevention. If it still happens, ANCP reports
  the conflict and waits, which is exactly the right behavior.
- A split-brain at the virtualization layer (two controllers, or a race in
  server-failover handling) — ANCP surfaces the symptom loudly, which is its
  job.

### 18.2 Operator hook

The protocol emits a structured event on every conflict start and end, with
`{private_ip, origins[]}`. Operators wire this to alerting; the design does not
specify a particular alerting stack (out of scope per §3).

## 19. Authentication

All ANCP messages flow exclusively inside the mesh's WireGuard tunnels, so
they inherit WireGuard's L3 transport authentication. Above that, ANCP adds
**sender-identity binding** and a **cross-origin forwarding ban** — together
they defeat a compromised-host forging another host's records.

### 19.1 Two-layer authentication

- **L3 transport** — WireGuard's per-peer `AllowedIPs` + Curve25519
  handshake. A peer that can't produce a valid handshake never sees an ANCP
  byte. A host is authenticated by its pubkey at the IP layer.
- **L4 origin binding** — every ANCP message carries `sender : HostID`, and
  the receiver asserts **`sender == wg-authenticated-peer-host-id`**. A host
  can only declare a message's Origin as its own HostID.

The check is psychological — the transport already authenticates — but it
matters for the relay/forwarding case (§13.2 step 6) where a record may arrive
from a relay peer whose HostID is not its origin: the relay forwards it but the
record's *origin* is the original writer, and the receiver, on apply, validates
that the **current** message's `sender` equals the record's origin **for any
record the relay is forwarding as the origin** — i.e. relays forward but do not
*vouch*. The simple rule:

> A receiver accepts a record `R` from sender `S` iff:
> - `S == R.origin`, OR
> - `S` is forwarding `R`, in which case `R` is *gossiped onward* but only
>   applied on receipt **if** the receiver already trusts `R.origin`'s key AND
>   the message signs `R` with `R.origin`'s signing key (ed25519, see §19.3).

The branch "S forwards R but the receiver hasn't seen R.origin's key yet" is
the bootstrap case — the receiver waits for the origin to advertise directly or
for anti-entropy to carry the origin's Membership Record, then the forwarded R
becomes applicable. Slightly more latency at join, same correctness.

### 19.2 Cross-origin forwarding ban (the security core)

The rule of §19.1 reduces to: **a host may only originate updates to its own
records**. Concretely:

- **Membership Records** — only the host named in `host_id` may publish or
  mutate its own Membership Record. No relay ever claims "host X is leaving"
  on X's behalf. This means a relay forwards X's record unchanged (signed by
  X, §19.3); it never synthesizes one.
- **Ownership Advertisements** — only the host named in `owner_host` may
  publish the advertisement. A relay forwards; it never invents an
  ownership claim for another host.

A compromised host can:

- Forge its own Membership Record (claim `leaving`, rotate keys, change
  endpoint). Detected: §14.2 fast-refute + operator alert.
- Forge its own Ownership Advertisement (claim to own a /128 it doesn't have).
  The local scan (§11) and the veth egress anti-spoof rule
  (`scripts/lib/atlas/private_network.py` rule 1) conspire to limit damage —
  a host can claim a /128 but if no VM is locally running on it the data
  plane drops at the veth on receive, exactly as it does today for a
  tampered guest. The §7.3 conflict mechanism also triggers: if the real owner
  is still advertising, the dropped-on-conflict path means the forger can DoS
  the /128 but cannot steal traffic.

A compromised host **cannot** forge another host's records because of the
sender-identity binding.

### 19.3 Optional ed25519 end-to-end signatures (defense in depth)

The transport-binding of §19.1 is sufficient under a threat model where every
host runs the same ANCP code and WireGuard key management is intact. For
defense-in-depth against a future where a relay is reachable from a host that
can't itself authenticate (e.g. a future reflection-attack surface), each
host additionally advertises an **ed25519 signing public key** in its
Membership Record and signs every record it originates. Verify is a per-apply
check; the keys ride Membership Generations like the wg pubkey (key rotation =
Generation bump). Recommended to ship from day one — the overhead is ~64 B
per record and a handful of µs per verify.

The signing key is generated alongside the wg keypair at first boot and stored
at `/etc/atlas-networkd/signing-key` (0600). It is **not** derived (Issue A
applies equally — a derived signing key's seed would be public).

### 19.4 Bootstrap trust (Issue A closeout)

- The first set of Membership Records is installed trust-on-first-use from the
  operator-signed seed file (`/etc/atlas-networkd/seed.json` signed with the
  provision key). The seed carries `(HostID, endpoint, wg_public_key,
  signing_public_key, mesh_address, generation)`.
- After first install, each origin updates only its own record at a higher
  Generation (§10.3).
- A re-provisioned host gets a new keypair and rejoins at a higher Generation
  as a normal §10 key rotation — peers drop the old pubkey, install the new.
  The HostID is stable; only the cryptomaterial rotates.

## 20. Future Work / Scalability

ANCP separates state semantics from the dissemination transport; §20 of the
original draft stands. This section adds the concrete ceilings.

### 20.1 The ~100–200 host ceiling (reasserted)

`spec/25-private-networking.md` flags the relay/hub-mesh as the deferred
answer past ~100–200 hosts; ANCP inherits that ceiling unchanged because the
**data plane** (N-1 wg peers per host, one `AllowedIPs` entry per cluster /128)
is the binding constraint, not the control plane:

- **Per-host wg-mesh peers** — N-1 at N hosts. WireGuard's hash-table peer
  lookup scales fine to ~10 000 peers, so the ~200 ceiling is operational, not
  WireGuard-internal.
- **Per-host AllowedIPs entries** — total /128s in the cluster (sum of all
  hosts' advertisement set sizes). At 100 hosts × 10 000 VMs ≈ 10⁶ entries
  per host's cryptokey routing table. This is the documented WireGuard
  ceiling neighborhood; past it, the relay/hub mesh (smaller peer-per-host
  fan-in) or a hierarchical Address Family split.
- **Per-host control traffic** — gossip O(gossip_fanout × msg_size) per round,
  anti-entropy O(N) summary size per round. At N=200 the anti-entropy summary
  is ~2 KB/s per peer pair — trivial against the wireguard-encrypted
  transport. Control traffic is not the binding constraint at any realistic
  Atlas scale today.

### 20.2 Scaling knobs (bounded at the control-plane layer)

| Axis | Knob | Effect |
| --- | --- | --- |
| Gossip traffic | `gossip_fanout` (default 3), `gossip_interval` (200 ms) | O(fanout · msg · interval) per host · s; double `gossip_interval` halves traffic at double latency. |
| Convergence latency | `anti_entropy_interval` (1 s) | Tighten for faster catch-up, loosen for steady-state traffic savings. |
| Failure detection | `suspect_timeout` (10 s, partition-sensitive) | Raise for partition-prone WANs; lower for tight DC floors. Bounded by §14.3 worst-case formula. |
| Per-host memory | `seen_cache_size` (10 000) | Raise for longer partitions covered without re-apply; bounded by RAM not protocol. |
| Data plane | relay/hub mesh (deferred, spec/25) | The only knob that breaks the ~200-host ceiling. |

### 20.3 Alternative future transports

The Membership/Ownership records are transport-agnostic (§20 original draft).
Future implementations may adopt a full mesh for small clusters (gossip
becomes sync-with-all-every-tick, removing the O(log N) latency) or an epidemic
broadcast tree (reducing per-host send rate from k to 1) without changing the
record semantics or §13.2 apply rules. ANCP is specified so that swapping the
dissemination layer is a transport-level change.

## 21. Conclusion

The original draft's §21 conclusion stands. This revision makes it precise:

Each compute host runs `atlas-networkd` and participates equally in a
decentralized eventually-consistent control plane. It maintains two
replicated datasets — Membership (how to reach other compute hosts) and
Ownership (which compute host currently owns each private IP) — using epidemic
gossip (§13) and anti-entropy reconciliation (§15). The cluster converges
without centralized coordination, with bounded observable inconsistency
(§17.2), with hard non-overlap invariant at each host (§16.3), and with a
crisp SWIM-with-Lifeguard failure-detection ladder that avoids false eviction
(§14).

Four correctness issues against the original draft are resolved by the
smallest possible changes — none of which disturb the architectural fixed
points (no controller, every host runs networkd, gossip + anti-entropy, no
leaders, WireGuard data plane, nftables per-VM isolation, ownership-only
state):

- **A** — keys self-generated, not derived (§7.1, §8).
- **B** — atomic whole-table `syncconf` + conflict-driven drop preserves
  per-host non-overlap; the hard two-push barrier becomes a soft sequencing
  bounded by one anti-entropy round (§16).
- **C** — per-origin full-set advertisements; generations compared only within
  an origin; conflicts drop and report, never elect (§7.2, §7.3, §13.2).
- **D** — SWIM-with-Lifeguard suspicion ladder, refute path,
  `suspect_timeout`/`ownership_grace` knobs built to avoid false eviction
  (§14).

The Atlas controller no longer computes routing, distributes configuration,
or performs remote execution. Its responsibility ends once a compute host has
been provisioned with its bootstrap contract (§8). By separating virtualization,
state replication, and packet forwarding into independent layers, the resulting
architecture is simpler, more resilient, horizontally scalable to the
data-plane ceiling, and easier to evolve over time.

---

## Appendix A. The four correctness fixes, summarised

| Issue | Where fixed | Smallest change |
| --- | --- | --- |
| A — key derivation leaks the mesh when records are gossiped. | §7.1, §8 | **Deferred.** Keys remain controller-derived (HKDF from HostID) per §7.1 rationale — the deployment keeps the controller as the trusted bootstrap root, and the derivation model gives zero-churn re-bootstrap. A future phase may move to self-generated keys. |
| B — non-overlap invariant becomes probabilistic under eventual consistency. | §16.3, §16.4 | Atomic, whole-table `syncconf` + conflict-driven drop. Each host stays internally unambiguous; cross-host transient disagreement is a self-healing blackhole, not misdelivery. Replaces the hard two-push barrier with soft sequencing bounded by one anti-entropy round. |
| C — cross-origin generation comparison would silently elect an owner. | §7.2, §7.3 | Per-origin full-set advertisement; generations compared only within an origin; union-of-latest is the effective table. Conflicts drop + report, never elect. |
| D — naïve membership removal false-evicts on partitions. | §14 | SWIM-with-Lifeguard: `alive → suspect → dead` with refute, timers knobbed by `suspect_timeout`, `ownership_grace` strictly longer than the suspicion window. |

No architectural fixed point (no controller, every host runs networkd, gossip +
anti-entropy, no leaders/route-reflectors/coordinators, WG data plane, nftables
tenant isolation, ownership-only state) is changed by any of the four.