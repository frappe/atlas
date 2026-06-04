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
When we move to bigger metal, we will revisit the addressing scheme. Note
this 15-VM ceiling is an *address* limit, independent of the vCPU
*placement* budget (`Atlas Settings.overprovision_factor`, see
[02-doctypes.md](./02-doctypes.md)) — whichever binds first stops placement.

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

Done once by `bootstrap-server.py`:

```
# /etc/sysctl.d/60-atlas.conf
net.ipv6.conf.all.forwarding = 1
net.ipv6.conf.default.forwarding = 1
net.ipv6.conf.all.proxy_ndp = 1
net.ipv4.ip_forward = 1
```

These four lines are load-bearing: with no bridge, the host *routes* between
its uplink and each per-VM tap, so forwarding must be on — for IPv6 (the
guest's public address) and for IPv4 (`net.ipv4.ip_forward`, so the host can
route the guests' private v4 out its uplink for NAT44 egress). This is the
reason the host-hardening step keeps forwarding enabled **in deliberate
violation of CIS 3.3.1** ("disable IP forwarding"), for both families —
turning it off makes every VM dark. There is no "scope it to the uplink"
half-measure: IPv6 has no clean global-off/per-interface-on split (toggling
per-interface `forwarding` also changes RA/autoconf), so `conf.all.forwarding`
is the operative switch and it stays on. Blast radius is contained at the
`inet atlas` nftables chains instead. The same `60-atlas.conf` file also
carries the CIS 3.3 network-hardening sysctls; see
[03-bootstrapping.md § Host hardening](./03-bootstrapping.md).

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
`vm-network-up.py` still adds the proxy-NDP entry — it costs nothing on
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
[`vm-network-up.py`](../scripts/vm-network-up.py) recreates the table, both
chains, and the masquerade rule idempotently at each unit-start, and
re-applies the IPv6 forwarding / proxy-ndp / `ip_forward` sysctls defensively.
This keeps each VM unit self-sufficient on cold boot — after a host reboot, the
first VM unit to start brings the whole scaffold back. Per-VM IPv6 forward
rules are added by the same script.

## Per-VM, on the host

Each VM gets its **own network namespace**, so a jail breakout cannot see the
host's interfaces, the uplink, or any other VM's tap. The VM's tap lives inside
that namespace; a **veth pair** bridges the namespace back to the host. The
guest contract is unchanged — it still uses `fe80::1` (on the tap, now inside
its namespace) as its gateway and sees only its own `/128`. The only difference
from a host-netns tap is one extra link-local hop across the veth, entirely
inside the host.

[`vm-network-up.py`](../scripts/vm-network-up.py), invoked by the systemd
unit's `ExecStartPre`, reads `network.env` (which carries `TAP_DEVICE`,
`VIRTUAL_MACHINE_IPV6`, `ATLAS_NETNS`, `HOST_VETH`, `NAMESPACE_VETH`,
`IPV4_HOST_CIDR`, `IPV4_GUEST_CIDR`) and:

1. Creates the namespace `ATLAS_NETNS` (clean re-create for known state).
2. Creates the veth pair and moves `NAMESPACE_VETH` into the namespace.
3. Enables IPv6 and IPv4 forwarding **inside the namespace** — it forwards
   between the veth (uplink side) and the tap (guest side) for both families,
   and namespaces have independent sysctls that default to off.
4. Inside the namespace: creates the tap with `vnet_hdr`
   (`ip tuntap add … mode tap vnet_hdr` — Firecracker's virtio-net activation
   calls `TUNSETOFFLOAD`, which requires `IFF_VNET_HDR`, or activation fails with
   `EBADF` and the guest boots with no NIC), assigns `fe80::1/64` (the guest's
   IPv6 gateway) and the host side of the per-VM /30 (`IPV4_HOST_CIDR`, the
   guest's IPv4 gateway) to it, and routes `VM_IPV6/128` to the tap.
5. Brings both veth ends up with transit addresses (`fe80::2`/`fe80::3` for v6;
   a `169.254.0.0/30` link-local pair for v4) and points the namespace's
   **default routes** (both families) at the host end, so guest egress flows out
   the veth toward the uplink.
6. On the host: routes `VM_IPV6/128` (v6) and the guest's v4 as a `/32` into the
   namespace via the veth, and adds the proxy-NDP entry for the VM on the uplink.
   The v4 route targets the guest address (`IPV4_GUEST_CIDR` without its mask),
   **not** `IPV4_HOST_CIDR` — that is a host address carried with a `/30` prefix,
   and `ip route` rejects a prefix whose host bits are set ("Invalid prefix for
   given prefix length"). The guest `/32` is the v4 analog of the v6 `/128`.
7. Adds two nftables forward rules matching `HOST_VETH` for IPv6 (the tap is no
   longer in the host namespace to match on). IPv4 egress needs no per-VM rule:
   the host-wide `100.64.0.0/16` masquerade rule (postrouting, created in the
   scaffold above) rewrites the guest's private v4 to the uplink address once it
   reaches the host across the veth.

It runs as `ExecStartPre`, not `ExecStartPost`: the jailer joins the namespace
via `--netns` and Firecracker attaches to the tap on startup, so the namespace
and the tap (with `vnet_hdr`) must exist before the jailer's `ExecStart` fires.
`ExecStartPre` runs to completion first, so this ordering holds.

[`vm-network-down.py`](../scripts/vm-network-down.py) is symmetric and
best-effort: it removes the proxy-NDP entry, the host route, then `ip netns del`
(which takes the tap and the namespace-side veth with it), the host-side veth,
and the nft rules.

## Inside the guest

The Ubuntu cloud image is patched **at image sync time** (not at
VM provision time) with a single systemd unit,
[`scripts/guest/atlas-network.service`](../scripts/guest/atlas-network.service).
It reads `/etc/atlas-network.env` (which `provision-vm.py` writes per-VM
containing `VIRTUAL_MACHINE_IPV6=...`, `VIRTUAL_MACHINE_IPV4=...` (the guest's
/30 CIDR), and `VIRTUAL_MACHINE_IPV4_GATEWAY=...` (the host side of the /30))
and runs:

```
ip link set eth0 up
ip -6 addr add ${VIRTUAL_MACHINE_IPV6}/128 dev eth0
ip -6 route add default via fe80::1 dev eth0
ip addr add ${VIRTUAL_MACHINE_IPV4} dev eth0
ip route add default via ${VIRTUAL_MACHINE_IPV4_GATEWAY} dev eth0
rm -f /etc/resolv.conf; echo "nameserver 2606:4700:4700::1111" > /etc/resolv.conf
```

The guest does **not** use SLAAC, DHCPv6, or DHCP. Static addressing from
`/etc/atlas-network.env` keeps the host-side routing trivial and avoids running
an RA / DHCP daemon on the host. DNS is the Cloudflare IPv6 resolver — v4-only
*destinations* are reached through the NAT, but DNS itself stays on v6, so no
DNS64 is involved.

The `rm -f` before the redirect is load-bearing: the Ubuntu cloud image ships
`/etc/resolv.conf` as a **symlink** to systemd-resolved's stub
(`../run/systemd/resolve/stub-resolv.conf`) and points the system resolver at
`127.0.0.53`. A bare `> /etc/resolv.conf` follows that symlink and writes into
the stub, which resolved owns — so the Atlas nameserver never wins and
`getaddrinfo()`/`apt` get an empty stub (zero upstreams, because Atlas configures
the network statically and never feeds resolved a `DNS=`). `sync-image.py` masks
`systemd-resolved.service` and replaces the symlink with a real file at build
time (steps 3a.1b / 3a.4b); the `rm -f` here re-asserts that at every boot so a
future image that re-introduces the symlink can't silently break DNS again. The
giveaway symptom: `dig @2606:4700:4700::1111 deb.debian.org` works but
`getent hosts deb.debian.org` fails.

Both egress families are proven end-to-end by the e2e suite from *inside* a
booted guest, using literal addresses so no DNS is involved: `curl -6` to an
IPv6 literal (`2606:4700:4700::1111`) exercises the routed-tap path + host IPv6
forwarding, and `curl -4` to an IPv4 literal (`1.1.1.1`) exercises the NAT44
masquerade. See the `phase5-ipv4-egress.sh` probe driven by
`virtual_machine_provisioning`.

## Verifying connectivity

End-to-end check from any IPv6-capable client: `ping6
<VM_IPV6>`. If that fails, walk the stack from the outside in. Most
"VM is unreachable" reports map to one of these:

| Symptom                                 | Likely cause                                                  | Check                                                              |
| --------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------ |
| Host `…:d001` answers, VM `…:dXXX` does not | VM address is outside the routable /124                       | `Server.ipv6_virtual_machine_range` must *contain* the host address. If it starts at `…:d000` and the host is `…:d001`, good. If it starts at `:::/124` (the /64 start), the carve is wrong — see below. |
| VM address is in the /124, still silent | proxy-NDP entry missing on the uplink                         | On the host: `ip -6 neigh show proxy` should list the VM address against `eth0` (or whatever `ip -6 route show default` reports as `dev`). |
| Proxy entry present, still silent       | No host route into the namespace                              | On the host: `ip -6 route` should show `<VM_IPV6>/128 via fe80::3 dev <HOST_VETH>`. Inside the namespace (`ip netns exec <ns> ip -6 route`) the same `/128` should point at the tap, and `default via fe80::2`. |
| Route present, VM unreachable, guest can't resolve its gateway | Tap created without `vnet_hdr`, or the namespace isn't forwarding | The tap is inside the namespace now: `ip netns exec <ns> ip -d link show <tap>` should list `tun … vnet_hdr on`. Also `ip netns exec <ns> sysctl net.ipv6.conf.all.forwarding` must be `1` (the namespace forwards veth↔tap). |
| Tap looks right, ping still drops       | nftables forward rules missing                                | On the host: `nft list table inet atlas` should show one ingress + one egress rule per live VM, matching `<HOST_VETH>` (not the tap). |
| Everything on the host looks right      | Guest didn't apply its address                                | In the guest console (firecracker log): look for `atlas-network.service` failures, or `ip -6 addr show eth0` showing no `<VM_IPV6>/128`. |
| IPv6 works, but IPv4 destinations time out (`curl -4 1.1.1.1` hangs) | NAT44 egress broken | On the host: `nft list chain inet atlas postrouting` should show the `100.64.0.0/16 … masquerade` rule, and `sysctl net.ipv4.ip_forward` should be `1`. In the guest: `ip -4 addr show eth0` should show a `100.64.x.x/30` and `ip -4 route show default` a route via the host side. |
| Guest reaches the host but not the IPv6 internet (`curl -6 2606:4700:4700::1111` hangs) | IPv6 egress broken | On the host: `sysctl net.ipv6.conf.all.forwarding` should be `1` and the per-VM forward rules present (`nft list table inet atlas`). In the guest: `ip -6 route show default` should be `via fe80::1 dev eth0`. |
| `ping`/`curl` to literal IPs work, but `apt update` / any hostname fails | DNS broken: systemd-resolved hijacked `/etc/resolv.conf` | In the guest: `dig @2606:4700:4700::1111 deb.debian.org` succeeds but `getent hosts deb.debian.org` fails. `ls -l /etc/resolv.conf` is a symlink to `…/stub-resolv.conf` and `cat` shows `nameserver 127.0.0.53` instead of the Cloudflare v6 line. Fix on a live guest: `systemctl disable --now systemd-resolved; rm -f /etc/resolv.conf; echo "nameserver 2606:4700:4700::1111" > /etc/resolv.conf`. Permanent fix is in the image (`sync-image.py` masks resolved + de-symlinks resolv.conf). |

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

Before the systemd unit moved `vm-network-up.py` to `ExecStartPre`,
firecracker's `ExecStart` won the race: it opened the tap fd first,
the kernel auto-created an `atlas-…` tap *without* `IFF_VNET_HDR`,
and firecracker's `TUNSETOFFLOAD` ioctl then failed with `EBADF`.
Firecracker logged a one-line warning and proceeded; the guest came
up with no working NIC. The fix in the unit is to create the tap
explicitly with `ip tuntap add … vnet_hdr` *before* firecracker
starts, which is why `vm-network-up.py` is an `ExecStartPre` step
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
