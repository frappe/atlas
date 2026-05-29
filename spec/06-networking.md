# Networking

Each VM gets one public **IPv6** address — that is its identity and its only
inbound path. For **outbound** traffic to IPv4-only destinations, each VM also
gets a private IPv4 that the host masquerades (NAT44). No inbound IPv4, no
per-VM public v4. No private network between VMs. No overlay.

## Why IPv6 for identity

DigitalOcean assigns each droplet a /64 IPv6 prefix, which is enough to give
every conceivable VM a unique routable address with no NAT. IPv4 from DO is
per-droplet — to give each VM its own *public* v4 we'd need paid floating IPs.
So the VM's address (the thing the outside world reaches, the thing we
allocate and record) is IPv6. IPv4 reachability is a separate, outbound-only
concern, solved below with host NAT — not by giving each VM a routable v4.

## IPv4 egress (NAT44)

Lots of the internet is still IPv4-only (package mirrors, APIs, registries).
An IPv6-only guest can't reach them. So each VM gets a **private** IPv4 on the
same `eth0`, a v4 default route, and the host **masquerades** that traffic out
its own public IPv4. This is egress-only: nothing reaches the VM *over* IPv4.

The private v4 is **derived from the VM's IPv6 address**, not separately
allocated — there is no v4 field on `Virtual Machine` and no v4 allocator.
[`derive_ipv4_link(ipv6_address)`](../atlas/atlas/networking.py) returns the
host and guest sides of a point-to-point **/30** inside a fixed
`100.64.0.0/16` supernet (RFC 6598 CGNAT space, chosen so it can't collide
with a Self-Managed host's own LAN). The /30 is indexed by the low bits of the
VM's IPv6 address, so a VM's v4 and v6 share an index:

```
VM ipv6 = …::2   ->  v4 link 100.64.0.8/30   host 100.64.0.9   guest 100.64.0.10
VM ipv6 = …::3   ->  v4 link 100.64.0.12/30  host 100.64.0.13  guest 100.64.0.14
```

The /16 holds 16384 /30 links — far more than any server's VM count (a DO /124
caps at 15 VMs). The guest uses the host side of its /30 as its IPv4 gateway,
exactly mirroring how it uses `fe80::1` as its IPv6 gateway. Because the
address is masqueraded at the uplink it never appears on the wire; it only has
to be unique per host.

## What the host actually gives us

This depends on the provider type.

### DigitalOcean

DO advertises a /64 to the droplet, but only a **/124 (16 addresses) is
usable** for onward routing — addresses outside that /124 are not reachable
through DO's network from elsewhere on the internet. This is a real-world
DO limit, not a Firecracker limit.

The routable /124 is the one **containing the droplet's own IPv6
address**, not the first /124 of the /64. For example, a droplet whose
public v6 is `2400:6180:100:d0:0:1:4ae1:d001` gets `…:d000/124` as the
usable range; addresses elsewhere in `2400:6180:100:d0::/64` are silently
dropped at DO's edge. The Python helper
[`carve_virtual_machine_range(host_address, prefix_cidr)`](../atlas/atlas/networking.py)
computes this for us at provision time.

So:

- `Server.ipv6_prefix` records the full /64 we got (informational).
- `Server.ipv6_virtual_machine_range` records the **/124** carved around
  the host address that we actually hand out from.
- VMs are addressed inside that /124.

Inside a /124 we have 16 addresses. The host uses one (typically `::1`),
which leaves 15 for VMs. That is enough for the size of droplet we're using
in this iteration (`s-2vcpu-4gb-intel` realistically fits 5–10 VMs anyway).
When we move to bigger metal, we will revisit the addressing scheme.

### Self-Managed

The operator tells Atlas, at provision time, exactly which prefix is
available for VM addresses. Atlas does not derive it and does not assume
any specific prefix length:

- `Server.ipv6_prefix` is informational — typically the full prefix
  routed to the host (e.g. a /64).
- `Server.ipv6_virtual_machine_range` is what Atlas actually allocates
  from. It can be a /124 (matching the DO model), a /96, an /80, a full
  /64, or anything else the operator's upstream has given them. The
  allocator below does not care about the length.

A Self-Managed host with an extra /64 routed to it lifts the 15-VM cap
that constrains DO droplets.

## Allocation

Sequential, scoped per server:

```
ipv6_virtual_machine_range = 2a03:b0c0:abcd:1234::/124
live allocations            = ::2, ::3, ::5      # ::4 was terminated earlier
next                        = ::4                # ::4 is back in the pool
```

`::1` is reserved for the host. We start at `::2`. The algorithm scans
existing `Virtual Machine.ipv6_address` rows for the server whose status is
not `Terminated`, and picks the lowest unused address inside
`ipv6_virtual_machine_range` (whatever its prefix length).

When the range fills up with live VMs, provisioning fails with "no IPv6
capacity". The operator either terminates old VMs (immediately releasing
their addresses) or provisions a new server. On a DigitalOcean /124 this
ceiling is 15; on a Self-Managed /64 it is effectively unbounded.

Terminated VMs release their address. The audit trail still lives in the
`Virtual Machine` row (status=Terminated, ipv6_address recorded at the
time it ran), so "which VM had this address on 2026-03-01?" is answered by
filtering on `creation`/`modified` — the field itself is not the index.

## MAC

Stable, derived from the UUID:

```
mac = "06:00:" + ":".join(format(b, "02x") for b in uuid.bytes[:4])
```

`06` sets the locally-administered bit. Two VMs would collide only if their
UUIDs share the first 4 bytes — practically impossible for UUID4.

## TAP device

`tap_device = "atlas-" + uuid_hex_no_dashes[:9]`. Linux `IFNAMSIZ` is 16
*bytes* including the null terminator, so usable interface-name length is
15: `atlas-` (6) + 9 = 15 exactly.

## Host-side configuration

Done once by `bootstrap-server.sh`:

```
# /etc/sysctl.d/60-atlas.conf
net.ipv6.conf.all.forwarding = 1
net.ipv6.conf.default.forwarding = 1
net.ipv6.conf.all.proxy_ndp = 1
net.ipv4.ip_forward = 1
```

`net.ipv4.ip_forward` lets the host route the guests' private v4 out its
uplink.

`proxy_ndp` is the trick that makes the DigitalOcean scheme work. Each
VM has its address routed to a per-VM tap device, but DO's upstream
router asks NDP "who has 2a03:b0c0:abcd:1234::2?" on the uplink (`eth0`).
With proxy NDP enabled and an explicit `ip -6 neigh add proxy` entry on
the uplink for each VM address, the host answers on the VM's behalf.
The upstream router delivers to the host MAC; the host's route table
sends it out the right tap.

On Self-Managed hosts where the entire `ipv6_virtual_machine_range` is
**routed** to the host (not advertised on-link), the upstream router
already knows where to send those packets and proxy-NDP is a no-op.
`vm-network-up.sh` still adds the proxy-NDP entry — it costs nothing on
a routed prefix and keeps the script identical across providers.

We also create one nftables table (`inet atlas`) with two chains: a `forward`
chain (filter, for the per-VM IPv6 rules) and a `postrouting` chain (nat) that
holds **one host-wide masquerade rule** for IPv4 egress:

```
inet atlas postrouting:  ip saddr 100.64.0.0/16 oifname <uplink> masquerade
```

The source match is the whole `100.64.0.0/16` supernet, so a single rule
covers every VM — there is no per-VM NAT rule and nothing to remove when a VM
is terminated. The table is **not** persisted to `/etc/nftables.conf`; instead
[`vm-network-up.sh`](../scripts/vm-network-up.sh) recreates the table, both
chains, and the masquerade rule idempotently at each unit-start, and
re-applies the IPv6 forwarding / proxy-ndp / `ip_forward` sysctls defensively.
This keeps each VM unit self-sufficient on cold boot — after a host reboot, the
first VM unit to start brings the whole scaffold back. Per-VM IPv6 forward
rules are added by the same script.

## Per-VM, on the host

[`vm-network-up.sh`](../scripts/vm-network-up.sh), invoked by the systemd
unit's `ExecStartPre`, reads `network.env` and:

1. Creates a tap device for the VM with `vnet_hdr` enabled
   (`ip tuntap add … mode tap vnet_hdr`). Firecracker's virtio-net
   activation calls `TUNSETOFFLOAD` on the tap fd, which requires
   `IFF_VNET_HDR` on the device — without it activation fails with
   `EBADF` and the guest boots with no working NIC.
2. Assigns `fe80::1/64` to the tap (so the guest can use `fe80::1` as its
   IPv6 gateway).
3. Assigns the host side of the per-VM /30 (`IPV4_HOST_CIDR` from
   `network.env`) to the tap, so the guest can use it as its IPv4 gateway.
   The connected route the /30 creates reaches the guest, so no explicit
   per-VM v4 route is needed.
4. `ip -6 route add VM_IPV6/128 dev TAP_DEVICE`.
5. `ip -6 neigh add proxy VM_IPV6 dev <uplink>`.
6. Adds two nftables forward rules for IPv6: ingress and egress. (IPv4 needs
   no per-VM rule — the host-wide masquerade rule above covers it.)

It runs as `ExecStartPre`, not `ExecStartPost`: firecracker attaches to
the tap on startup, so the tap must exist with `vnet_hdr` before
firecracker's `ExecStart` fires. If `vm-network-up.sh` ran after, the
kernel would auto-create a tap without `vnet_hdr` when firecracker opens
it, then `vm-network-up.sh`'s idempotent `ip link del` would yank the
device out from under firecracker mid-boot.

[`vm-network-down.sh`](../scripts/vm-network-down.sh) is symmetric and
best-effort.

## Inside the guest

The Firecracker CI Ubuntu image is patched **at image sync time** (not at
VM provision time) with a single systemd unit,
[`scripts/guest/atlas-network.service`](../scripts/guest/atlas-network.service).
It reads `/etc/atlas-network.env` (which `provision-vm.sh` writes per-VM
containing `VIRTUAL_MACHINE_IPV6=...`, `VIRTUAL_MACHINE_IPV4=...` (the guest's
/30 CIDR), and `VIRTUAL_MACHINE_IPV4_GATEWAY=...` (the host side of the /30))
and runs:

```
ip link set eth0 up
ip -6 addr add ${VIRTUAL_MACHINE_IPV6}/128 dev eth0
ip -6 route add default via fe80::1 dev eth0
ip addr add ${VIRTUAL_MACHINE_IPV4} dev eth0
ip route add default via ${VIRTUAL_MACHINE_IPV4_GATEWAY} dev eth0
echo "nameserver 2606:4700:4700::1111" > /etc/resolv.conf
```

The guest does **not** use SLAAC, DHCPv6, or DHCP. Static addressing from
`/etc/atlas-network.env` keeps the host-side routing trivial and avoids running
an RA / DHCP daemon on the host. DNS is the Cloudflare IPv6 resolver — v4-only
*destinations* are reached through the NAT, but DNS itself stays on v6, so no
DNS64 is involved.

## Verifying connectivity

End-to-end check from any IPv6-capable client: `ping6
<VM_IPV6>`. If that fails, walk the stack from the outside in. Most
"VM is unreachable" reports map to one of these:

| Symptom                                 | Likely cause                                                  | Check                                                              |
| --------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------ |
| Host `…:d001` answers, VM `…:dXXX` does not | VM address is outside the routable /124                       | `Server.ipv6_virtual_machine_range` must *contain* the host address. If it starts at `…:d000` and the host is `…:d001`, good. If it starts at `:::/124` (the /64 start), the carve is wrong — see below. |
| VM address is in the /124, still silent | proxy-NDP entry missing on the uplink                         | On the host: `ip -6 neigh show proxy` should list the VM address against `eth0` (or whatever `ip -6 route show default` reports as `dev`). |
| Proxy entry present, still silent       | No host route into the tap                                    | On the host: `ip -6 route` should show `<VM_IPV6>/128 dev atlas-<...>`. |
| Route present, VM unreachable, guest can't ARP its gateway | Tap created without `vnet_hdr` (firecracker auto-created it before `vm-network-up.sh` ran) | On the host: `ip -d link show <tap>` — should list `tun … vnet_hdr on`. If absent, the VM's virtio-net activation failed silently and the guest came up with no working NIC. Cause: the script ran as `ExecStartPost` instead of `ExecStartPre`. |
| Tap looks right, ping still drops       | nftables forward rules missing                                | On the host: `nft list table inet atlas` should show one ingress + one egress rule per live VM. |
| Everything on the host looks right      | Guest didn't apply its address                                | In the guest console (firecracker log): look for `atlas-network.service` failures, or `ip -6 addr show eth0` showing no `<VM_IPV6>/128`. |
| IPv6 works, but IPv4 destinations time out (`curl -4 1.1.1.1` hangs) | NAT44 egress broken | On the host: `nft list chain inet atlas postrouting` should show the `100.64.0.0/16 … masquerade` rule, and `sysctl net.ipv4.ip_forward` should be `1`. In the guest: `ip -4 addr show eth0` should show a `100.64.x.x/30` and `ip -4 route show default` a route via the host side. |

### Historical bug: the carve

Before [`atlas/atlas/networking.py`](../atlas/atlas/networking.py)
took `host_address` as well as `prefix_cidr`, the carve returned the
first /124 of the /64 (`2400:6180:100:d0::/124`). On a droplet whose
own v6 was `2400:6180:100:d0:0:1:4ae1:d001` the *routable* /124 is
`…:d000/124` — the carve was off by a wholly different sub-prefix
and VMs were assigned addresses DO silently dropped at its edge. The
host pinged fine (its own address was always routable); the VM was
opaque. The lesson: **the host's own address is the only datum that
tells you where DO put the routable window** — never derive the
/124 from the /64 alone.

### Historical bug: vnet_hdr

Before the systemd unit moved `vm-network-up.sh` to `ExecStartPre`,
firecracker's `ExecStart` won the race: it opened the tap fd first,
the kernel auto-created an `atlas-…` tap *without* `IFF_VNET_HDR`,
and firecracker's `TUNSETOFFLOAD` ioctl then failed with `EBADF`.
Firecracker logged a one-line warning and proceeded; the guest came
up with no working NIC. The fix in the unit is to create the tap
explicitly with `ip tuntap add … vnet_hdr` *before* firecracker
starts, which is why `vm-network-up.sh` is an `ExecStartPre` step
even though it touches host routing.

## What we do not do

- **No inbound IPv4.** IPv4 is egress-only via host NAT44. The VM has no public
  v4 and no port-forward/DNAT — nothing on the internet can open a connection
  to it over v4. Inbound is IPv6-only.
- **No per-VM egress IP / no NAT64.** Every VM on a host shares the host's
  public v4 via one masquerade rule. We do not give VMs distinct egress v4s and
  we do not run NAT64/DNS64 (that would be a layer above Atlas).
- No per-VM firewall. The guest is on the public internet over IPv6. Tightening
  this is on the [roadmap](./09-roadmap.md).
- No floating/reserved IPv6. If a VM is archived its address is retired.
- No DDoS mitigation. DO does what DO does at the edge.
