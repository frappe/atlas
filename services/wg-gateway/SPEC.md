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
setup.sh      Installer (wireguard-tools, nftables, wg0, daemon, boot firewall)
gatewayd.py   Standalone gateway API (peers, config, health)
systemd/      Service units (reapply wg0 and run the API on boot)
```

The daemon listens on the gateway mesh address at port 8080. Central reaches
it through the `<gateway>.<wildcard-domain>` proxy route with the gateway API
token Bearer credential. `peers.json` persists the peer list on disk; every
change rewrites `peers.conf` and `gateway.nft` and applies them with
`wg setconf` and `nft -f`. Atlas installs the daemon and registers the proxy
route, then never touches peer state.

## Interfaces

- `setup.sh` reads `GATEWAY_MESH`, `LISTEN_PORT`, `REGION_ID`, and `DAEMON_TOKEN`.
- `/opt/atlas/wg-gateway/peers.conf` holds the `wg setconf` interface and peers.
- `/opt/atlas/wg-gateway/gateway.nft` holds the `atlas_wg_gateway` table.
- `GET /config` on the daemon reports the gateway public key to Atlas.

## Addressing

Client addresses are `fdac | region 16 | tenant 32 | reserved 0 (32) |
client 32`. Tenant access is tenant-wide: a client reaches the
`fdaa:<region>:<tenant>::/64` of its own tenant. The layout helpers live in
`atlas/service/core/wg_gateway/address.py`.

## Ownership

Atlas owns the gateway VM, the peer list, and both state files. The gateway
owns only the translation. VM-level firewalling stays with each VM.
