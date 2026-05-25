# Networking

Each VM gets one public **IPv6** address. No IPv4 in the guest. No private
network. No overlay.

## Why IPv6 only

DigitalOcean assigns each droplet a /64 IPv6 prefix, which is enough to give
every conceivable VM a unique routable address with no NAT. IPv4 from DO is
per-droplet — to give each VM its own v4 we'd need NAT or paid floating IPs.
For the building block we sidestep the v4 question entirely. A future
"egress" layer can NAT64 from above Atlas.

## What DigitalOcean actually gives us

DO advertises a /64 to the droplet, but only a **/124 (16 addresses) is
usable** for onward routing — addresses outside that /124 are not reachable
through DO's network from elsewhere on the internet. This is a real-world
DO limit, not a Firecracker limit.

So:

- `Server.ipv6_prefix` records the full /64 we got (informational).
- `Server.ipv6_virtual_machine_range` records the **/124** carved from the
  /64 that we actually hand out from.
- VMs are addressed inside that /124.

Inside a /124 we have 16 addresses. The host uses one (typically `::1`),
which leaves 15 for VMs. That is enough for the size of droplet we're using
in this iteration (`s-2vcpu-4gb-intel` realistically fits 5–10 VMs anyway).
When we move to bigger metal, we will revisit the addressing scheme.

## Allocation

Sequential, scoped per server:

```
ipv6_virtual_machine_range = 2a03:b0c0:abcd:1234::/124
allocations on this server  = ::2, ::3, ::5      # ::4 was archived earlier
next                        = ::6
```

`::1` is reserved for the host. We start at `::2`. The algorithm scans
existing `Virtual Machine.ipv6_address` rows for the server (including
archived ones — addresses are not reused) and picks the next unused address.

When the /124 fills up, provisioning fails with "no IPv6 capacity". The
operator either archives old VMs (releasing addresses on the next bump of
the scheme — out of scope here) or provisions a new server.

We deliberately do **not** reuse addresses from archived VMs in this
iteration. Reuse complicates audits ("which VM had this address on
2026-03-01?") and we have plenty of capacity for the building block.

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
```

`proxy_ndp` is the trick that makes the whole scheme work. Each VM has its
address routed to a per-VM tap device, but DigitalOcean's upstream router
asks NDP "who has 2a03:b0c0:abcd:1234::2?" on the uplink (`eth0`). With
proxy NDP enabled and an explicit `ip -6 neigh add proxy` entry on the
uplink for each VM address, the host answers on the VM's behalf. The
upstream router delivers to the host MAC; the host's route table sends it
out the right tap.

We also create one nftables table (`inet atlas`) with one `forward` chain.
The table is **not** persisted to `/etc/nftables.conf`; instead
[`vm-network-up.sh`](../scripts/vm-network-up.sh) recreates it
idempotently at each unit-start and re-applies the IPv6 forwarding /
proxy-ndp sysctls defensively. This keeps each VM unit self-sufficient on
cold boot — after a host reboot, the first VM unit to start brings the
scaffold back. Per-VM forward rules are added by the same script.

## Per-VM, on the host

[`vm-network-up.sh`](../scripts/vm-network-up.sh), invoked by the systemd
unit's `ExecStartPost`, reads `network.env` and:

1. Creates a tap device for the VM.
2. Assigns `fe80::1/64` to the tap (so the guest can use `fe80::1` as its
   gateway).
3. `ip -6 route add VM_IPV6/128 dev TAP_DEVICE`.
4. `ip -6 neigh add proxy VM_IPV6 dev <uplink>`.
5. Adds two nftables forward rules: ingress and egress.

[`vm-network-down.sh`](../scripts/vm-network-down.sh) is symmetric and
best-effort.

## Inside the guest

The Firecracker CI Ubuntu image is patched **at image sync time** (not at
VM provision time) with a single systemd unit,
[`scripts/guest/atlas-network.service`](../scripts/guest/atlas-network.service).
It reads `/etc/atlas-network.env` (which `provision-vm.sh` writes
per-VM containing `VIRTUAL_MACHINE_IPV6=...`) and runs:

```
ip link set eth0 up
ip -6 addr add ${VIRTUAL_MACHINE_IPV6}/128 dev eth0
ip -6 route add default via fe80::1 dev eth0
echo "nameserver 2606:4700:4700::1111" > /etc/resolv.conf
```

The guest does **not** use SLAAC or DHCPv6. Static addressing from
`/etc/atlas-network.env` keeps the host-side routing trivial and avoids
running an RA daemon on the host.

## What we do not do

- No IPv4 in the guest. Reaching v4-only services on the internet is a
  future problem.
- No per-VM firewall. The guest is on the public internet. Tightening this
  is on the [roadmap](./09-roadmap.md).
- No floating/reserved IPv6. If a VM is archived its address is retired.
- No DDoS mitigation. DO does what DO does at the edge.
