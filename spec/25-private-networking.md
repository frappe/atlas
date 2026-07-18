# Private networking (the WireGuard host mesh)

Every VM gets a **private `fdaa::` address** on a per-tenant `/48`, carried across
hosts by a **WireGuard mesh that runs on the hosts, not in the guests**. Each host
peers with every other Active host over the hosts' public IPv6 endpoints; a guest
sends plain IPv6 to its tap and the host encapsulates. Tenant isolation and source
anti-spoof are enforced by **host nftables at the per-VM veth**, where each VM's
source is physically attributable (its own netns + veth).

This is the private-plane sibling of the public layers: [06-networking.md](./06-networking.md)
gives each VM a public `/128` for inbound-from-the-world; this chapter gives each VM a
private `fdaa::` `/128` for VM-to-VM (and, when a VM goes "dark", its *only* address).
Read [06-networking.md](./06-networking.md) first — this states only where the private
plane differs. The full design rationale (the 5-way design pass, the three isolation
holes an adversarial verifier closed, the 100×1000 scale analysis) lives in
`llm/references/private-networking-host-mesh.md`; this is the shipped subset.

> **Relationship to the customer gateway ([19](./19-vpn-broker.md)) and the management
> tunnel ([21](./21-tunnel.md)).** All three use WireGuard, but they are distinct
> planes. The **customer gateway** ([19](./19-vpn-broker.md)) is a customer's dial-in to
> this private plane — it terminates on a **gateway VM on the mesh** (not a host), where
> every customer is one `[Peer]` on a single shared `wg0`, and lands the client as a
> `/128` in its own tenant `/48` (see [The customer gateway](#the-customer-gateway--external-dial-in-to-the-mesh)
> below). The management tunnel is Central's *control-plane* dial-in (`wg0`,
> hub-and-spoke). The device this chapter builds — `wg-mesh` — is a full mesh among the
> hosts carrying the VM *data* plane. All three share the fixed UDP port `51820`, so the
> management-firewall's one `udp dport 51820 accept` covers them all.
>
> **Hosts stay off the internet.** A design invariant that this gateway model preserves:
> only a **handful of VMs with static public IPs** (the reverse proxy, the TCP proxy, the
> customer gateway) face the internet. The *hosts* peer host↔host over their public IPv6
> for the mesh, but they run **no customer-facing listener** — customer ingress lands on
> a VM, never on a host.

## The address plan — host-independent

```
fdaa : TTTT TTTT : RRRR : VVVV VVVV VVVV VVVV
 16       32        16          64
 ULA    HKDF(tenant) region     HKDF(VM UUID)
```

- `fdaa::/16` — the fixed ULA tag (mirrors fly.io's 6PN; leaves the rest of `fd00::/8`
  free and clear of the tunnel supernet).
- **32-bit tenant id** → a per-tenant `/48`, the isolation boundary
  ([`derive_tenant_prefix`](../atlas/atlas/networking.py)). Derived from the `Tenant`
  name (which **is** the Central `Team.name` — a naming series, *not* a UUID, so the
  seed hashes the name's bytes, not `uuid.UUID(name)`).
- **16-bit region** (bits 48–63) — 0 for a single-region deployment, so a VM reads
  `fdaa:T:T:0:V:V:V:V`. Frozen at creation.
- **64-bit VM part** — `HKDF(VM UUID)`
  ([`derive_private_address`](../atlas/atlas/networking.py)). Birthday-safe past any
  tenant's VM count, and **host-independent**: a pure function of `(tenant, VM)`, so a
  migrated VM keeps its private address **byte-for-byte** (this is the property that
  collapses migration's networking leg — see [24](./24-vm-migration.md) and below).

**No host bits anywhere.** The address is not allocated and not stored as a source of
truth — it is derived wherever it is needed, exactly like `derive_mac` / `derive_tap`.
The `Virtual Machine.private_address` field is a legible **denorm**, refreshed in
`before_validate`; it is empty for a VM with no tenant (an operator-created VM stays
off the private plane entirely).

The reserved **infra `/48`** (`fdaa:0:0::/48`, all-zero tenant bits, never HKDF-derivable
for a real tenant) holds the proxy's tap and each host's own mesh address.

## The host mesh

- **Device** `wg-mesh` in each host's **root netns** (invisible to every guest netns —
  stronger isolation than an in-guest model), MTU 1420, owning `ip -6 route fdaa::/16`.
- **Keys** are **derived from the Server UUID**
  ([`derive_host_wireguard_keypair`](../atlas/atlas/networking.py), a real Curve25519
  base-point multiply verified byte-for-byte against `wg pubkey`), never stored: the
  whole desired mesh reconstructs from the `Server` table, so a re-bootstrap re-derives
  the same identity with zero peer churn. The public key + the host's own
  `fdaa:0:0:<idx>::1` mesh address are denormed onto `Server`
  (`wireguard_public_key`, `mesh_address`) for legibility. The private key is injected
  to the host over the root-SSH layer, into `/etc/atlas-host-mesh.key` (0600), never an argv.
- **Peer set** = every *other* Active `Server`. Each peer's `AllowedIPs` is the
  enumerated set of `/128`s of the VMs currently on that host (all tenants) **plus** the
  peer's own infra mesh `/128` (§2.4 of the design — so the host↔host bus can dial it).
  Every `/128` lives on exactly one host, so the sets are non-overlapping and
  longest-prefix match resolves to one peer — no eBPF, no host-encoded address, no
  separate routing table.
- **Reconcile** — [`reconcile_host_mesh()`](../atlas/atlas/host_mesh.py), controller-
  over-**host**-SSH (`connection_for_server`, the root layer — the mesh is a host
  fabric), mirroring the proxy reconcile but **converging, never log-and-skip**: a
  skipped push is a *partition* of the forwarding fabric, so failures are collected and
  re-raised, and the job retries. It reads live `wg show wg-mesh dump`, canonicalizes
  both sides, and pushes a full `wg syncconf` on drift. **Fake-provider Servers are
  skipped** (no real host to SSH), so the reconcile is a clean no-op on a test fleet.
- **Host-side bring-up** — `host-mesh.service` (a boot oneshot, gated on
  `/etc/atlas-host-mesh.env`) re-asserts the device on reboot via
  [`scripts/lib/atlas/host_mesh.py`](../scripts/lib/atlas/host_mesh.py): create the
  device, pin MTU, assign the infra address, load the pushed peer config, and **set the
  derived key LAST** — `wg syncconf` from a key-less config *clears* the interface key,
  so key-after-syncconf is load-bearing (verified on a real host).

**Triggers** (all enqueued after-commit so a mesh push failure never rolls back the
lifecycle transaction; the converging reconcile + a `*/5 * * * *` scheduler backstop
sweep bring the fabric to match):

1. a host reaching `Active` (end of `finish_provisioning`) — reconciles the whole mesh;
2. a VM provision / terminate — the VM's `/128` joins / leaves its host's `AllowedIPs`;
3. a **migration cutover** — the one path that must be **sequenced, not converging**:
   WireGuard requires non-overlapping `AllowedIPs`, so the VM's `/128` is
   removed-from-source **then** added-to-target under the migration lock
   ([`sequenced_migration_cutover`](../atlas/atlas/host_mesh.py)). Runs on **both**
   keep- and change-address migrations — the private plane moves with the VM regardless
   of what happens to the public `/128`.

## Tenant isolation + anti-spoof (host nftables)

Because each VM has its own netns + veth, the source is physically attributable at the
veth — so **nftables suffices; eBPF is rejected**. The plane is **fail-closed**: a
single terminal `ip6 daddr fdaa::/16 drop`, appended once at scaffold time
(`bootstrap-server.py`), makes the private plane default-deny **without** flipping the
whole forward chain's policy (public / NAT44 / reserved-IP traffic stays under
`policy accept`). Every per-VM rule is allow-by-exception, `nft insert`ed (head) **above**
that drop, by [`scripts/lib/atlas/private_network.py`](../scripts/lib/atlas/private_network.py)
from `vm-network-up.py` as a pure function of the VM's row (design §4b):

1. **anti-spoof** — a packet into the mesh from this guest's veth MUST carry exactly this
   VM's `/128` source (catches a forged cross-tenant *or* infra source in one);
2. **same-tenant egress** — this VM may reach its own `/48`;
3. **infra-destination** — this VM may reach (and reply to) the proxy/resolver in the
   infra `/48` (without it the terminal drop black-holes every proxied reply);
4. **cross-host delivery** — a packet decap'd from a peer host (`iifname wg-mesh`) into
   this VM's veth.

Teardown (`vm-network-down.py` → `remove_private_network`) sweeps these by handle on
the **private** `/128` + veth — the fix for the confirmed teardown bug where the
public-only sweep would leave stale rules pointing at a recycled veth (a cross-tenant
leak), and a complete no-op for a dark VM that has no public `/128` at all.

## The "dark" VM (no public traffic)

`Virtual Machine.public_networking` (Check, **default 1** — today's behavior preserved
exactly). When cleared, the VM is **dark**: `allocate_ipv6` is skipped (no public
`/128`, no `/124` slot consumed, no proxy-NDP / public forward rule), and the VM's only
identity is its `fdaa::` address. A dark VM **requires a tenant** (its only identity
derives from the tenant `/48`), enforced at insert. `egress_nat44` (Check, default 1) is
an **independent** toggle — a dark VM still gets IPv4 egress for `apt`/security updates
by default (its NAT44 `/30` indexes off the private `/128` instead of the absent public
one); clear it for a truly air-gapped VM. The proxy reaches a dark VM over the mesh:
`Subdomain._denormalize_address` dials `ipv6_address` for a public VM (zero change) and
`private_address` for a dark one.

## The customer gateway — external dial-in to the mesh

The mesh is VM-to-VM. The **gateway** is the mesh's one external door: a customer runs
stock `wg-quick`, dials the region's **gateway VM**, and lands **inside their tenant's
`/48`** — reaching every VM in their VPC by its stable `fdaa:` address, over an encrypted
L3 link, from any client including an **IPv4-only** one. This replaces the host-terminated
VPN broker that [19](./19-vpn-broker.md) originally described. The client behaves like a
**dark VM that lives at the customer's premises**: a `/128` inside the tenant `/48`, routed
by the mesh the fabric already runs. The full design (the two-mechanism isolation proof,
the fly.io 6PN lineage, the scale math, the host-verified eBPF program) lives in
`llm/references/customer-vpc-vpn.md`; this is the shipped subset.

### Two decisions this design turns on

- **One interface per gateway, every customer a `[Peer]` on it — never an interface per
  customer.** A gateway VM carries thousands of peers on a single `wg0` (fly.io's own
  shared-gateway model). Enrolling customer #10 001 is `wg set wg0 peer <pk> allowed-ips
  <client>/128` — a hash-table insert, not an `ip link add`. This is the whole fix to the
  "100 hosts × 10 000 VMs ⇒ ~100 k tunnels" explosion the host-terminated broker implied.
- **The internet touches VMs, never hosts.** The only public-v4 ingress is the gateway
  VM's `:51820`, exactly as the reverse proxy ([12](./12-proxy.md)) and TCP proxy
  ([17](./17-tcp-proxy.md)) are the only public doors. Hosts keep their existing posture:
  they peer host↔host for `wg-mesh` but run **no customer-facing listener**.

### The gateway VM — the proxy VM's sibling

A new operator-owned infra VM role, modelled on the proxy: `Virtual Machine.is_gateway`, a
**fixed** reserved public v4 (so the customer's `Endpoint` never moves) and a fixed infra
`fdaa:` address in the reserved **infra `/48`**, and a `reconcile_gateway(server)` that
syncs desired peers → live `wg0` over **guest-SSH** (the proxy idiom — the gateway is a
guest, *not* the host-SSH mesh path). One gateway **per region**; a single-region
deployment (region = 0, this chapter's default) therefore runs **one** gateway VM, and a
second is a pure horizontal shard past a wg peer-count / bandwidth ceiling — no design
change. The gateway is an ordinary mesh guest: it decrypts a customer peer, and its host's
`wg-mesh` does the cross-host delivery, so a customer with 40 VMs across 12 hosts dials
**one** gateway and reaches all 40 — that is what makes this a *VPC*, not a one-VM tunnel.

### The client's address — a `/128` inside the tenant `/48`

The client's overlay address is a real address inside the tenant's `/48`, so the tenant's
VMs treat the laptop exactly like a sibling VM (and the return path is automatic). The
4th hextet is structurally `0x0000` for VMs; a client sets it to **`0x0001`** and derives
its low 48 bits from the client-config UUID — clients and VMs are disjoint sub-ranges of
the same `/48` **by construction**, no allocator, no collision:

```
VM      :  fdaa : T T : 0000 : V V V V     (4th hextet 0x0000)   ← the mesh plan, unchanged
CLIENT  :  fdaa : T T : 0001 : C C C C     (4th hextet 0x0001, low 48 = HKDF(client uuid))
```

`derive_client_address(tenant, client_config)` is one more HKDF derivation in the same
discipline as `derive_private_address` — host-independent, reconstructible from the row,
so the laptop keeps its VPC address regardless of which gateway terminates it. Because it
shares the tenant `/48` bits, `client & /48 == tenant prefix` — the identity the
destination guard leans on.

### Isolation — pinning the customer to their `/48` (no per-customer state)

Everyone shares one `wg0`, so isolation is **per-source**, not per-interface, and needs
**zero per-customer state**. Two mechanisms, one per direction:

1. **Source — pinned by WireGuard itself (free, per-peer).** Each peer's host-side
   `AllowedIPs` is that customer's own client `/128`. WireGuard's cryptokey routing drops,
   **in the kernel before nftables runs**, any packet from that peer whose inner source
   isn't that exact `/128`. A customer cannot forge another tenant's or a VM's source, even
   sharing `wg0` with 10 000 others — `AllowedIPs` is matched per decrypting peer key.
2. **Destination — confined to the source's own `/48` by a static eBPF program.** Since a
   client and its VMs share the tenant `/48` bits, the confinement rule is exactly *accept
   iff `saddr` and `daddr` have the same `/48`*; combined with mechanism 1 (source is
   always the client's own tenant), "same `/48` as source" ≡ "the client's own tenant." A
   single static `same_48` tc program on the gateway's `wg0` ingress expresses this for
   **all** customers at once — enrolling a customer never touches it.

   **This needs eBPF, not nftables — host-verified.** nftables **cannot compare two packet
   fields to each other** (masked or not; verified failing to parse on a real kernel-6.8
   host); it only compares a field to a constant or a set. A ~6-instruction eBPF program
   *can*, and was compiled, verifier-passed, JIT'd, and attached to a real `wg0` (the exact
   program + the two attach gotchas — `wg0` is L3 so don't parse an `ethhdr`; the load
   section must be `tc` — are in the reference). The program is **fail-closed** (drops any
   non-`fdaa` source or destination too, so the public internet, the infra `/48`, and the
   gateway host itself are all unreachable through it). An nft concatenated interval set is
   the documented fallback (same security, but one set element per customer).

A third rule closes host-local delivery on the gateway (sshd/Frappe bind `::`): a single
`inet … input`-hook `iifname "wg0" drop`. Like the eBPF guard, it is attached **once** at
gateway bake and never changes per customer.

### The one mesh delta — the client `/128` for the return path

For a VM to reach the laptop back, every other host must route the client `/128` to the
**gateway VM's host**. That is one `AllowedIPs` addition to the gateway host's stanza,
riding the existing `reconcile_host_mesh()` converging delta-push — identical to how a
VM's `/128` is advertised. **It must be withdrawn on revoke** (the exact teardown-bug
class this chapter already flags for `/128`s — reconcile on teardown, not only on enroll).
This is the *only* change the host mesh sees; the gateway does everything else as a plain
mesh guest emitting `fdaa::`.

### Keys, DocType, durability

- **Keys.** The client generates its own keypair and sends only the public half (its
  private key never touches Atlas). The gateway mints its own `wg0` keypair once (`0600`,
  kept on the gateway); its public key is read from the gateway, **not stored per row** —
  one key per gateway, shared by every peer.
- **DocType — `VPN Peer`** (one row per customer device), deliberately *not*
  [19](./19-vpn-broker.md)'s `VPN Tunnel`: `tenant` (immutable — the scope), `gateway`
  (denorm target), `label`, `status` (`Pending` → `Active` → `Revoked`),
  `client_public_key` (immutable), and the computed `client_address` (`derive_client_address`),
  customer-side `allowed_ips` (the tenant `/48`), and `endpoint` (`<gateway-v4>:51820`).
  **No `interface_name`, `slot_index`, `listen_port`, or per-row `server_public_key`** —
  the gateway has one `wg0`, one port, one key. That deletion *is* the fix.
- **Controller.** `request_vpc_access(tenant, client_public_key, label)` (whitelisted,
  owner-scoped + Central-callable, [16](./16-central.md)): validate the key, resolve the
  region gateway, insert the row, `reconcile_gateway(gateway)` (convergent/idempotent like
  `reconcile_proxy` — read live `wg show wg0 dump`, `wg syncconf` on drift), push the
  client `/128` into the mesh, and return the copy-paste config. `revoke()` reconciles the
  gateway with the row `Revoked` (drops the peer) and withdraws the mesh `/128`. Losing the
  last VM does **not** auto-revoke (a customer may hold a tunnel to an empty VPC).
- **Durability.** Every peer is reconstructible from its row; a rebuilt gateway re-mints
  its key and `reconcile_gateway` re-pushes every Active peer. Nothing per-customer is
  persisted on a host. Bringing a peer up touches no default-deny policy, no host, no
  sshd, and no isolation state, so **no [21](./21-tunnel.md)-style armed auto-revert is
  needed** — a bad customer config only fails that customer's own handshake.

### Client setup

Stock `wg-quick`: generate a keypair, request access with the **public** key, drop the
returned values into `/etc/wireguard/tenant-vpc.conf`, `wg-quick up`. The client sees two
`AllowedIPs` — the customer side is `fdaa:T:T::/48` (route the whole VPC out the tunnel;
advisory, the customer may edit it) and `Endpoint` is the **gateway's** v4. If a hostile
customer edits their side to `::/0`, nothing changes on the security side — the gateway
still accepts only their own `/128` as a source and the `same_48` guard still drops any
cross-tenant destination.

## Phasing — what ships now vs. later

- **Phase 1 (shipped):** the host mesh + universal private addressing + the per-VM
  isolation rules + the terminal default-deny. The proxy still dials public.
- **Phase 2 (groundwork shipped, host side deferred):** the `Subdomain` denorm switch is
  in place, but the proxy actually *joining* the mesh (its infra `/48` address + the
  `iifname + exact-saddr` cross-tenant exception) is deferred — it needs a live proxy on
  a mesh host to prove.
- **Phase 3 (deferred):** the fully-dark VM path end-to-end, gated on Phase 2.
- **Phase 4 (deferred):** a `<vm>.<tenant>.internal` DNS resolver.
- **Phase 5 (designed, not built):** the customer gateway VM (`is_gateway`, one `wg0` with
  N customer peers, the static `same_48` eBPF guard, `VPN Peer` DocType +
  `request_vpc_access`/`revoke` + `reconcile_gateway`, and the one client-`/128` mesh
  delta). Depends only on the mesh (Phase 1, shipped). The `same_48` mechanism is settled —
  compiled, verifier-passed, JIT'd and attached on a real `wg0` (2026-07-02); the remaining
  host fact is the full live-`wg0` return-routing e2e, incl. a second customer of a
  different tenant on the same `wg0` proving the shared interface isolates. Full design +
  the eBPF program: `llm/references/customer-vpc-vpn.md`.

**Non-goals (v1):** **in the host mesh (Phases 1–4)** no eBPF and no host-encoded address
(nftables at the veth suffices); no guest-side WireGuard **in a tenant guest** (the
gateway VM is an *infra* guest Atlas owns — Phase 5); no relay/hub mesh (the ~100–200-host
answer, deferred); no `.internal` DNS; no public-`/128` mobility; no automatic key rotation
(re-provision = rotate); `tenant` is not a permission scope. **The customer gateway (Phase
5)** does use a static eBPF guard on the gateway's `wg0` — the one place the plane can't
attribute a source at a per-tenant veth, so nftables' inability to compare two packet
fields forces it (see [The customer gateway](#the-customer-gateway--external-dial-in-to-the-mesh)).

## Testing

The derivations + config render are unit-tested (`atlas/tests/test_private_networking.py`);
the controller/Task wiring is unit-tested (`atlas/tests/test_private_networking_wiring.py`);
the host-side nft rules + bring-up commands are unit-tested
(`scripts/lib/atlas/test_private_network.py`, `test_host_mesh.py`). The host facts only two
real cross-edge hosts can prove — the mesh coming up over UDP/51820 across the provider
edge, host↔host reachability over the tunnel, same-tenant cross-host guest reachability,
and the cross-tenant isolation drop — live in the **two-droplet**
[`host_mesh`](../atlas/tests/e2e/use_cases/host_mesh.py) e2e use case, invoked directly
(like migration):

```
bench --site atlas.tests.local execute atlas.tests.e2e.use_cases.host_mesh.run_smoke  # two hosts, mesh + reachability
bench --site atlas.tests.local execute atlas.tests.e2e.use_cases.host_mesh.run        # + guest VMs, isolation drop
```
