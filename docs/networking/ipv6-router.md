# IPv6 router

The IPv6 router is a [network gateway VM](index.md#network-gateway-vms) that maps public addresses from one block to VM mesh addresses. The address bits carry the mapping, so the router needs no VM list or connection state.

## Why Atlas uses a router

Some providers attach an IPv6 block to one machine or network interface. [AWS delegates `/80` prefixes to an interface](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-prefix-eni.html). [Scaleway routes a `/64` to one instance](https://www.scaleway.com/en/docs/instances/how-to/use-flexips/#ipv6-prefix-and-slaac).

Moving one tenant VM to another Metal host does not move that whole block with it. Atlas therefore keeps the block on a router VM and gives each tenant VM a derived `/128`.

The VM can move while its mesh and public addresses stay the same. WG Mesh finds its new host, and the router keeps sending packets to the same mesh address.

| Network | Best fit |
| --- | --- |
| Provider attaches a whole block to one host | Use the Atlas IPv6 router to keep guest `/128` addresses stable across VM moves. |
| Network routes each `/128` to the host that claims it, such as an operator-managed VXLAN | Use a [static direct IPv6 pool](public-ips.md#static-direct-pools). The router is not needed for that constraint. |

## How the address maps

```text
public  = <block prefix> | reserved, all zero | tenant 24 bits | VM 20 bits
private = fdaa | region 16 bits | tenant 32 bits | VM 64 bits
```

For block `2001:db8:1:2:3::/80`, mesh address `fdaa:1:0:abcd::5` maps to `2001:db8:1:2:3:a:bcd0:5`.

The block must be inside `2000::/3`, with a prefix from `/3` to `/84`. A VM whose tenant ID is 2^24 or more, or whose VM number is 2^20 or more, gets no public address.

::: details Try the IPv6 address mapping in Python

This example derives the public address and recovers the tenant and VM number from it:

```python
from ipaddress import IPv6Address, IPv6Network

block = IPv6Network("2001:db8:1:2:3::/80")
mesh = IPv6Address("fdaa:1:0:abcd::5")
region = 1

tenant = (int(mesh) >> 64) & 0xFFFFFFFF
vm = int(mesh) & 0xFFFFFFFFFFFFFFFF
assert tenant < 1 << 24 and vm < 1 << 20

public = IPv6Address(int(block.network_address) | (tenant << 20) | vm)
suffix = int(public) - int(block.network_address)
decoded_tenant, decoded_vm = divmod(suffix, 1 << 20)
decoded_mesh = IPv6Address(
    (0xFDAA << 112) | (region << 96) | (decoded_tenant << 64) | decoded_vm
)
print(public, decoded_mesh)
# 2001:db8:1:2:3:a:bcd0:5 fdaa:1:0:abcd::5
```

:::

Atlas computes the `/128` when it creates a routed allocation. The router's eBPF program performs the same mapping in both directions for packets.

## Packet path

The [gateway packet example](wg-mesh/gateways.md#how-the-packet-changes) shows the source and destination at each hop. On the inbound path this router changes the destination to the VM mesh address and keeps the client source. On the return path it changes the mesh source to the public address.

A `tc` eBPF program on the router's `eth0` ingress rewrites the address and fixes the checksum. For an ICMPv6 error, it also rewrites the address inside the quoted packet. It drops:

- A packet to a block address with a reserved bit set.
- A mesh source from another region, or a tenant or VM number that does not fit the public layout.
- A translated packet that is not TCP, UDP, ICMPv6, or a fragment of one of them.

## Block unassigned traffic

The router can calculate a VM address from any valid address in its block. It does not look up Atlas allocations. Delivery has a second guard in WG Mesh: a packet with an outside client source reaches a tenant VM only when that VM has a gateway return route for the client address.

Atlas adds the VM's `2000::/3` route to this router when a routed IPv6 allocation is attached. It removes the route on detach. Without that route, WG Mesh drops the forwarded client packet even if the public address has valid mapping bits. The same route sends replies back through the router.

::: info Two separate checks
The router checks address shape and packet format. WG Mesh checks whether the target VM has a return route. A mathematically valid address alone is not an attached public address.
:::

## Prepare the router VM

Atlas creates an [IPv6 Router Server service VM](../region/service-vms.md) with no public IPv4 address. It waits until the VM leaves draft state and Metal holds the VM.

The setup job then makes the VM a network gateway, attaches the public IPv6 pool through the provider, and gives the router a `2000::/3` route through its host. It waits for SSH, installs the hashed router package, and sets the service record to `Active`.

Each router owns one pool. A region can run several routers for separate blocks.

For each routed tenant VM, Atlas derives a public address from its mesh address and points the VM's `2000::/3` route at the router's mesh address. A pool cannot be archived while routed allocations still use it. A failed setup records its phase and needs operator action.

## Install by hand

To install by hand, run this as root on a tenant-0 gateway VM that owns the block. It is safe to run again:

```sh
REGION_ID=1 PUBLIC_IPV6_PREFIX=2001:db8:1:2:3::/80 ./setup.sh
```

Check it with:

```sh
systemctl status atlas-ipv6-router.service
tc filter show dev eth0 ingress
nft list table ip6 atlas_ipv6_router
```

## Limits and recovery

| Limit | Reason |
| --- | --- |
| One router per VM | A VM has one `2000::/3` route. |
| One fragment header only | The program cannot fix the checksum safely behind other extension headers. |
| The router has no public address of its own | The program runs on ingress only. |

If routed traffic fails, check in order: the Atlas allocation, the router VM and its service, the VM's `2000::/3` route, then the mesh path. `Active` in Atlas does not prove forwarding still works.

::: details Source code and tests

- [Router specification](../../services/ipv6-router/SPEC.md) defines the translation boundary.
- `services/ipv6-router/`: [address mapping](../../services/ipv6-router/bpf/address.h), [packet program](../../services/ipv6-router/bpf/router.c), `bpf/packet.h`, `nftables/router.nft`, `systemd/atlas-ipv6-router.service`, and `setup.sh`.
- [Atlas router provisioner](../../atlas/service/core/ipv6_router/provisioning.py) sets up the VM and installs the package.
- [Address derivation](../../atlas/service/core/ipv6_router/address.py) implements the Atlas allocation side of the mapping.
- [WG Mesh return-route check](../../services/wg-mesh/bpf/maps.h) rejects foreign-source packets without a gateway route.
- [Public IP service](../../atlas/metal_server/core/public_ip_service.py) derives and attaches routed addresses.
- [Router provision tests](../../atlas/service/core/ipv6_router/test_provisioning.py) check setup order and failures.

:::
