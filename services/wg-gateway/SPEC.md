# WireGuard gateway component specification

[Root specification](../../SPEC.md)

## Purpose

Each WireGuard gateway lets customer devices reach the private `fdaa::/16` VMs
of their own tenant. The client holds an `fdac::/16` address, the gateway
SNATs it to its own tenant-0 mesh address, and the WG Mesh carries the packet
to the VM. Return traffic is restored by conntrack. No custom eBPF is
involved; the gateway is WireGuard plus one nftables table.

## Layout

```text
setup.sh      Installer (wireguard-tools, nftables, wg0, boot firewall)
systemd/      Service unit (reapplies wg0 peers and nftables on boot)
```

Atlas renders the peer-dependent state (`peers.conf`, `gateway.nft`) on every
peer change and pushes it over SSH. See
`atlas/service/core/wg_gateway/peers.py`.

## Interfaces

- `setup.sh` reads `GATEWAY_MESH` and `LISTEN_PORT`.
- `/opt/atlas/wg-gateway/peers.conf` holds the `wg setconf` interface and peers.
- `/opt/atlas/wg-gateway/gateway.nft` holds the `atlas_wg_gateway` table.
- `wg show wg0 public-key` reports the gateway public key to Atlas.

## Addressing

Client addresses are `fdac | region 16 | tenant 32 | reserved 0 (32) |
client 32`. Tenant access is tenant-wide: a client reaches the
`fdaa:<region>:<tenant>::/64` of its own tenant. The layout helpers live in
`atlas/service/core/wg_gateway/address.py`.

## Ownership

Atlas owns the gateway VM, the peer list, and both state files. The gateway
owns only the translation. VM-level firewalling stays with each VM.
