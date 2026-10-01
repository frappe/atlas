# Atlas WireGuard gateway

The gateway forwards customer WireGuard traffic to VMs of the same tenant without changing the client source address. See the [component specification](SPEC.md) and [gateway VM design](../../docs/networking/wg-mesh/gateways.md).

Client addresses use `fdac | region 16 | gateway ID 16 | tenant ID 32 | client ID 32 | zero 16`. The gateway owns `fdac:<region>:<gateway ID>::/48` and assigns its `::1/128` address to `wg0`.

The tc eBPF programs check tenants on packets entering `wg0` and replies entering `eth0`. Atlas adds a return route through the gateway mesh address to each VM with **Accessible via WireGuard Gateway** enabled. The route is scoped `wireguard-gateway`, so it lives only in the VM namespace on the host and the routes inside the VM never change.

Run `setup.sh` on a privileged network gateway VM with `REGION_ID`, `GATEWAY_ID`, `GATEWAY_MESH`, `LISTEN_PORT`, `JWKS_URL`, `GWGATEWAY_AUDIENCE`, and `JWKS_ISSUERS`. The installer compiles the eBPF object and starts the WireGuard and API services.

Check `wg show wg0`, `ip -6 addr show dev wg0`, `ip -6 route show dev wg0`, and `tc filter show dev wg0 ingress`. Check the eligible VM's `fdac` route and WG Mesh gateway route when replies fail.
