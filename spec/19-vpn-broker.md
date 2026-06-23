# The VPN broker (WireGuard tunnels)

Atlas brokers **WireGuard** tunnels: a VM's owner asks Atlas for a tunnel to
their VM, and Atlas provisions one on demand and hands back a ready client
config. Once the client brings the tunnel up it can reach **that one VM's public
IPv6 — and nothing else** — over an encrypted L3 link, from any client, including
an **IPv4-only** one.

This is the private-ingress sibling of the public-facing layers: the reverse
proxy ([12-proxy.md](./12-proxy.md)) and TCP proxy ([17-tcp-proxy.md](./17-tcp-proxy.md))
expose *one service* to *the world*; the VPN broker gives *the owner* private,
all-port reach to *their own VM*. It reuses the per-VM network namespace and the
`inet atlas` nftables table from [06-networking.md](./06-networking.md) — read
that first; this chapter states only where the tunnel differs and why.

## Why a tunnel

A VM's identity is its public IPv6 ([06-networking.md](./06-networking.md)). That
is reachable from the v6 internet, but:

- **A client may not have IPv6.** An IPv4-only laptop or CI runner cannot dial a
  v6 address at all. The tunnel's **outer transport is the server's public IPv4**
  (the droplet's own v4, which every host has), so any client can connect; the
  **inner** traffic it carries is the VM's v6. A v4-only client thus reaches a
  v6-only VM.
- **Public exposure is not private access.** Reaching a port over the tunnel is
  independent of whether that port is exposed to the public internet. The owner
  gets every port their VM actually serves on its address — including ones a
  future per-VM public firewall ([09-roadmap.md](./09-roadmap.md)) would keep off
  the public internet — without opening them to the world.
- **Scoped to exactly one VM.** The tunnel is not a route into anything wider: it
  reaches the target VM's `/128` and the host **drops** anything else. The client
  cannot hop through it to another VM, to the host, or to the internet.

What the tunnel deliberately is **not**: a way to make the VM a peer in the
owner's own WireGuard mesh (that would terminate inside the guest — see
[Why host-terminated](#why-host-terminated-not-in-guest)), nor a private network
*between* VMs (the standing no-overlay non-goal in
[06-networking.md](./06-networking.md) holds — this is Atlas↔VM, not VM↔VM).

## The shape

**One WireGuard interface per tunnel, terminated on the host, in the host's root
network namespace.** Atlas owns it end to end; the guest is never touched and
holds no tunnel state.

- **Outer endpoint:** `<server-public-v4>:<port>`. The interface listens on a
  per-tunnel UDP port on the host's public IPv4. There is no host input firewall
  (the `inet atlas` table is `forward` + `nat` only — see
  [06-networking.md](./06-networking.md)), so the port is reachable with no extra
  INPUT rule.
- **Inner reach:** the VM's `/128`. The host root netns already holds a
  `<vm-v6>/128 via <host-veth>` route into the VM's namespace
  (`vm-network-up.py`), so a packet the interface decrypts with destination
  `<vm-v6>` is routed straight into the right VM with no new routing.
- **Overlay link:** a `/127` from a ULA supernet gives the host side and the
  client side a private v6 address each, so the VM has a return address to reply
  to (`<vm-v6>` → `<client-overlay-v6>` routes back into the interface).

The interface name is `wg-<11 hex of the tunnel UUID>` — `wg-` (3) + 11 = 14,
IFNAMSIZ-safe like `derive_tap`, and distinct from the `atlas-…` veth/tap names.

### Data plane

```
client (any v4)  --UDP/IPv4-->  <server-v4>:<port>   [host root netns]
                                       │  wg-<id> decrypts
                                       │  inner: src=<client-overlay-v6> dst=<vm-v6>
                                       ▼
                          host route <vm-v6>/128 via <host-veth>
                                       ▼
                          VM netns ── tap ── guest eth0  (binds <vm-v6>)
```

- **client → VM:** the client encapsulates `src=<client-overlay-v6>,
  dst=<vm-v6>` to `<server-v4>:<port>`. `wg-<id>` decrypts, WireGuard's
  cryptokey routing checks the inner source is in the peer's `AllowedIPs`
  (`<client-overlay-v6>/128` ✓), and the host routes `<vm-v6>` into the VM.
- **VM → client:** the guest replies `src=<vm-v6>, dst=<client-overlay-v6>` out
  `eth0` → host. The host routes `<client-overlay-v6>/128 dev wg-<id>`; the
  interface encrypts to the one peer whose `AllowedIPs` covers it, back out over
  v4.

### Isolation — only your VM, no leaks

Three independent layers, defense in depth:

1. **The client's `AllowedIPs` is `<vm-v6>/128`.** A well-behaved client routes
   only the VM through the tunnel.
2. **The host nft rule pins the interface to the VM.** Because the interface is
   1:1 with the tunnel, its name uniquely identifies it, so two rules in the
   existing `inet atlas forward` chain are exact:
   - `iifname "wg-<id>" ip6 daddr <vm-v6> accept`
   - `iifname "wg-<id>" drop`

   Cryptokey routing only governs what the host *sends back* to a peer; it does
   **not** restrict the *destination* of a decrypted inbound packet. So without
   this rule a client could set `dst=<other-vm-v6>` and the host would route it.
   The `drop` closes that: anything from this interface that is not destined to
   this VM — another VM, the host, the internet — is dropped. This mirrors
   `reserved_ip_nat.py`'s forward accept and survives the future per-VM-firewall
   policy flip ([09-roadmap.md](./09-roadmap.md)).
3. **The network namespace.** Even granting a hostile inner packet, the host
   routes only `<vm-v6>/128` into a netns that contains exactly one VM; there is
   no path from one VM's netns to another.

### Why host-terminated, not in-guest

Terminating WireGuard *inside* the guest would make isolation structurally free
(the tunnel exists only in that one guest), but Atlas rejects it for the same
reason it rejected configuring a Reserved IP's anchor inside the guest
([06-networking.md](./06-networking.md)): it breaks the provider-agnostic,
Atlas-owns-nothing-in-the-guest contract. Concretely:

- **The v4 endpoint forces host plumbing anyway.** A guest has no public v4 —
  only the host does. In-guest termination would *still* need a host DNAT of
  `<server-v4>:<port>` into the guest, **plus** per-tunnel config pushed into the
  guest. That is strictly more than host-termination, not less.
- **Lifecycle durability.** Rebuild / restore / clone rewrite the guest disk;
  in-guest config would be lost or duplicated. Host-side config lives in host
  state and survives a rebuild exactly like Reserved IP's `RESERVED_IPV4`. A
  tunnel can even be provisioned or revoked while the VM is **Stopped**, coming
  alive on the next boot.
- **Atlas-enforced isolation and revocation.** Isolation is the netns + nft rule
  Atlas owns, not something a tenant can misconfigure out of, and Atlas can
  revoke without trusting the guest.

The free-isolation advantage of the in-guest model is already matched by
host-side netns + the one nft rule, so there is no net win to terminating in the
guest. (If a future feature genuinely wants *the VM to join an external
WireGuard mesh as a peer*, that is a different, guest-terminated feature — not
this broker.)

## Addressing and allocation

Per-server, sequential, in the spirit of `allocate_ipv6` / `derive_ipv4_link`
([06-networking.md](./06-networking.md)):

- A **tunnel slot** is the lowest unused index among the server's non-`Revoked`
  tunnels. The index yields both the **listen port** (`TUNNEL_PORT_BASE +
  index`) and a **`/127` overlay link** carved from `ATLAS_TUNNEL_SUPERNET` (a
  ULA prefix). The overlay is private and never appears on the public wire — like
  the NAT44 egress `/30`, it only has to be unique per host.
- The slot is released when the tunnel is `Revoked` (its index returns to the
  pool), exactly as a Terminated VM releases its `/128`.

## Keys and custody

The **client generates its own keypair and sends only its public key.** The
client's private key never touches Atlas.

The **host generates its own keypair, on the host**, exactly as Atlas already
treats a guest's SSH **host** keys — host-resident crypto identity, not mirrored
into the Frappe DB. This is deliberate: `Task.variables` is a plaintext, audited,
immutable field a VM's owner can read for their own VM (`permissions.py`), so
routing a private key through a Task — the only channel `run_task` has — would
leave a private key in an audit row and on the host process table. Generating on
the host keeps the private key in **one** place: a `0600` file under the VM dir,
which `wg set` reads by path (never the command line).

- [`vm-tunnel.py`](../scripts/vm-tunnel.py)`--action up` mints the host keypair
  the first time (idempotent — a re-apply reuses the existing `<tunnel>.key`, so
  the client's config never goes stale) and returns the **public** key.
- `request_tunnel(virtual_machine, client_public_key)` stores that returned
  `server_public_key` on the `VPN Tunnel` row (the public identity) and validates
  the client's submitted key with
  [`atlas/atlas/wireguard.py`](../atlas/atlas/wireguard.py)`.is_valid_public_key`
  before dispatching, so a malformed key fails in the controller, not on the host.
- The response carries the host `server_public_key`, the `endpoint`
  (`<server-v4>:<port>`), `allowed_ips` (`<vm-v6>/128`), the assigned
  `client_address` (`<client-overlay-v6>/128`), and a copy-paste client config
  template + setup instructions ([Client setup](#client-setup)).

Principle #2 (Frappe is the source of truth) holds the same way it does for SSH
host keys: the row records the public identity; the private material is
host-resident and re-issued (a new tunnel) if the host is lost.

## Durability

A tunnel is durable host state, reconstructible from the Frappe row, exactly like
the rest of a VM's host networking:

- The host config is persisted under the VM directory: a `<tunnel>.env` metadata
  sidecar (`0644`) and a `<tunnel>.key` private-key file (`0600`) under
  `/var/lib/atlas/virtual-machines/<uuid>/tunnels/`.
- [`vm-network-up.py`](../scripts/vm-network-up.py) re-applies every persisted
  tunnel **after** it brings up the netns/veth, so tunnels survive a cold boot;
  [`vm-network-down.py`](../scripts/vm-network-down.py) tears them down with the
  rest of the VM's networking.
- A **live** add/remove (no reboot) is one Task,
  [`vm-tunnel.py`](../scripts/vm-tunnel.py) (`--action up|down`), which both
  writes/removes the persisted config **and** applies/removes the live `wg` + nft
  state — the same attach-now-plus-persist-for-reboot pattern as
  [`vm-reserved-ip.py`](../scripts/vm-reserved-ip.py).

## The `VPN Tunnel` DocType

One row per tunnel, modelled on [`Reserved IP`](./02-doctypes.md#reserved-ip):

| Field | Notes |
| --- | --- |
| `virtual_machine` | the target VM; immutable after insert |
| `server` | denormalized from the VM (task dispatch + slot allocation scope); immutable |
| `tenant` | attribution, from the VM |
| `status` | `Pending` → `Active` → `Revoked` |
| `transport` | `public-ipv4` today; the seam for `private-vpc` later (see below) |
| `client_public_key` | the client's WireGuard public key (immutable) |
| `server_public_key` | host-side public key, returned by the host `up` Task and stored here (the private half stays host-only) |
| `slot_index` | the per-server slot (drives `listen_port` + overlay); immutable |
| `listen_port` | the allocated UDP port |
| `interface_name` | `wg-<id>` |
| `client_address` | `<client-overlay-v6>/128` assigned to the client |
| `endpoint` (computed) | `<server-v4>:<listen_port>` |
| `allowed_ips` (computed) | `<vm-v6>/128` |

Controller methods, audited as Tasks:

- `request_tunnel(virtual_machine, client_public_key)` (module-level,
  whitelisted): validate the client key, allocate the slot, insert the row, run
  `vm-tunnel.py --action up` (which mints the host key and returns its public
  half), store `server_public_key`, and return the client config. Owner-scoped,
  and Central-callable as the service user (the [16-central.md](./16-central.md)
  pattern, like `provision.create_vm`).
- `revoke()`: run `vm-tunnel.py --action down`, set `Revoked`. Skips the host
  Task for a Terminated VM whose networking is already gone (the
  [`Reserved IP.detach`](./02-doctypes.md#reserved-ip) rule). A VM `terminate()`
  revokes the VM's tunnels, like it detaches its Reserved IP.

## Client setup

The client needs the `wireguard-tools` package and an IPv4 path to the server.
Generate a keypair, request the tunnel with the public key, drop the returned
values into a config, and bring it up:

```sh
# 1. Generate a keypair (private key stays on this machine).
wg genkey | tee privatekey | wg pubkey > publickey

# 2. Ask Atlas for a tunnel, passing the PUBLIC key. The response carries
#    server_public_key, endpoint, allowed_ips and client_address.

# 3. Write /etc/wireguard/atlas.conf:
cat > /etc/wireguard/atlas.conf <<EOF
[Interface]
PrivateKey = <contents of privatekey>
Address    = <client_address>          # e.g. fd00:…::2/128

[Peer]
PublicKey  = <server_public_key>
Endpoint   = <endpoint>                 # <server-v4>:<port>
AllowedIPs = <allowed_ips>              # <vm-v6>/128
PersistentKeepalive = 25               # keep the UDP path open through NAT
EOF

# 4. Bring it up, then reach the VM at its public IPv6.
wg-quick up atlas
ssh root@<vm-v6>
```

`PersistentKeepalive` keeps the path open when the client is behind NAT (common
for a v4 client). The same instructions are returned inline by `request_tunnel`.

## The private-network seam (future)

The note in the task that birthed this feature — *use the public address now,
leave space for a private VPC later* — is one function:
`tunnel_endpoint_address(server)` returns `Server.ipv4_address` today and a
private VPC address once Atlas and its servers share a VPC. The `transport`
field records which (`public-ipv4` now, room for `private-vpc`), so the swap is a
new enum value plus that one function — no schema churn and no change to the
inner/isolation model.

## Not in this iteration

- **Desk UI and the real-droplet e2e** land in a follow-up: an operator form +
  buttons, and a `vpn_tunnel` use case that, on a live droplet, completes a
  handshake, reaches the owned VM, **proves it cannot reach a second VM** (the
  isolation host fact), then revokes.
- **No multi-VM / mesh tunnel.** One tunnel reaches one VM (the no-overlay
  non-goal).
- **No v4 *into* the VM over the tunnel.** Inner reach is the VM's v6 `/128`; a
  guest service must bind the VM's address or `::` (a loopback-only service is
  not reachable, as for any client).
- **No per-tunnel bandwidth or connection accounting.**
