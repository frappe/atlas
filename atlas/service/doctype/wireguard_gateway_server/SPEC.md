# WireGuard Gateway Server specification

[Service module specification](../../SPEC.md) · [Gateway component](../../../../services/wg-gateway/SPEC.md)

## Lifecycle

Atlas names records `wg-gateway-NNN`. Creation reserves a tenant-0 VM with a public IPv4 address and privileged mesh access. Provisioning enables the network gateway role, installs the gateway with the numeric record suffix as `GATEWAY_ID`, registers the proxy route, and reads the daemon public key. The listen port is fixed at creation.

Pending and interrupted provisioning is retried each minute. Installation failure records the phase and error. Archive removes the proxy route, terminates the VM, and marks the gateway Archived. The existing server sync withdraws the gateway's scoped return route from the VMs that opted in; the routes leave only the VM namespace on each host.

## Return routes

Each Active gateway owns one `fdac` `/48` return route through its mesh address. A VM opts in with **Actions > Enable WireGuard Gateway Access**, which installs the current route set. The accessible flag, the routes, and the gateway routes are virtual fields that read the VM's Metal network, so the routes are the stored opt-in. Each route carries the `wireguard-gateway` scope: Metal converges it only in the VM namespace on the host, and the guest metadata never lists it, so the routes inside the VM never change. The existing server sync keeps every opted-in VM's route set current as gateways are added and archived; the accessible flag turns off when the last route leaves.

## Access

Central uses the [gateway peer API](../../../../services/wg-gateway/SPEC.md) with an Ed25519 JWT. Only System Managers with System User accounts can create or archive a gateway.
