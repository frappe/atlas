# Atlas WireGuard gateway

The WireGuard gateway gives customer devices access to the private IPv6
addresses (`fdaa::/16`) of their own tenant's VMs. Each customer gets an
address from `fdac::/16`:

```text
fdac | region 16 bits | tenant 32 bits | reserved, all zero, 32 bits | client 32 bits
```

Access is tenant-wide: a client reaches `fdaa:<region>:<tenant>::/64`.

## Packet path

```text
Customer (fdac, WireGuard tunnel to gateway public IPv4:port)
  -> gateway wg0 -> nftables forward (tenant check) -> SNAT to gateway mesh fdaa address
  -> eth0 -> Metal host -> WG Mesh -> tenant VM
VM reply -> gateway mesh -> conntrack restores fdac -> wg0 -> customer
```

The gateway performs SNAT when forwarding into the mesh, so the VM sees the
gateway's tenant-0 mesh address. The VM firewall still applies.

## Code

| Path | Content |
| --- | --- |
| `setup.sh` | Installation on Ubuntu 24.04. |
| `systemd/atlas-wg-gateway.service` | Reapplies the `wg0` interface, routes, and nftables on boot. |
| `peers.conf`, `gateway.nft` (on the VM) | Atlas-rendered desired state, replaced on every peer change. `peers.conf` carries the interface section, so `wg setconf` alone restores the key and port. |

## Setup

Atlas installs the gateway. See
[WireGuard Gateway Server](../../atlas/service/doctype/wireguard_gateway_server/SPEC.md).

To install by hand, run this command as root on a tenant-0 gateway VM:

```sh
REGION_ID=1 GATEWAY_MESH=fdaa:1::99 LISTEN_PORT=51820 ./setup.sh
```

`setup.sh` installs WireGuard and nftables, generates the gateway keypair
unless one exists, creates `wg0`, and starts `atlas-wg-gateway.service`. You
can run it again; the keypair is kept.

## Checks

```sh
wg show wg0
nft list table ip6 atlas_wg_gateway
ip -6 route show dev wg0
```

## Limits

| Limit | Reason |
| --- | --- |
| Tenant-wide access only | Per-VM restriction is not implemented; the VM firewall is the second layer. |
| One listen port per gateway | The port is set when the gateway is created. |
