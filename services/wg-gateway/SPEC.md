# WireGuard gateway component specification

[Root specification](../../SPEC.md) · [Overview](README.md)

## Purpose

One gateway forwards customer `fdac` packets to the same tenant's `fdaa` VMs. It keeps the client source address. Each eligible VM routes replies through the gateway's mesh address.

## Interfaces

`setup.sh` compiles `bpf/gateway.c` as a tc object and installs the `systemd/` services. The `daemon/` package owns `peers.json` and applies `peers.conf` with `wg setconf`.

The installer takes `REGION_ID`, `GATEWAY_ID`, `GATEWAY_MESH`, `LISTEN_PORT`, `JWKS_URL`, `GWGATEWAY_AUDIENCE`, and `JWKS_ISSUERS`. The daemon binds to the gateway mesh address and validates Ed25519 JWTs for the `atlas-wg-gateway:<region>` audience. It accepts issuer prefixes `central` and `atlas:<region>` and scopes `*`, `peers:*`, `peers:read`, `peers:update`, and `gateway:read`.

## Address and packet contract

The client address is `fdac | region 16 | gateway ID 16 | tenant ID 32 | client ID 32 | zero 16`. The gateway has `fdac:<region>:<gateway ID>::1/128` on `wg0` and routes its `/48` there. WireGuard allows each peer's single `/128`.

The `wg0` ingress program requires a client source for this gateway and a destination with the same region and tenant in `fdaa`. The `eth0` ingress program checks the tenant and gateway ID of replies to `fdac`. Both drop mismatches. No address translation occurs.

Atlas owns the gateway VM and its network gateway role. The daemon owns peers. Atlas's existing server sync adds each active gateway's `/48` route, scoped `wireguard-gateway`, to every VM that holds an opted-in route set. The scope keeps the route in the VM namespace on the host: Metal converges it there and passes it to the WG Mesh return-route map, but the guest metadata never lists it, so the routes inside the VM never change.
