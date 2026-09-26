# Service module specification

[Atlas app specification](../SPEC.md)

Behavior: [Service VMs](../../docs/region/service-vms.md). This module runs Atlas services on tenant-0 VMs. A service is not a tenant workload.

## Types

| Type | Owns |
|---|---|
| `ProxyServer` (DocType) | One proxy node and its VM, at most five active |
| `CargoServer` (Single) | The regional Cargo VM |
| `IPv6RouterServer` (DocType) | One router VM and its pool, `ipv6-router-NNN` |
| `WireGuardGatewayServer` (DocType) | One gateway VM, its daemon credential, and its proxy route, `wg-gateway-NNN` |
| `service_package` | Publishes a package when its digest changes |
| `core/proxy`, `core/cargo`, `core/ipv6_router`, `core/wg_gateway` | Provisioning |

## Shared rules

- Each VM is created through `VirtualMachineService` as a privileged tenant-0 VM. It needs a reserved tenant-0 IPv4 allocation.
- A job requeues `Pending` records every minute.
- A failure sets `Failed` with the phase and message. Nothing replaces the VM automatically.
- A site file lock guards each record. Code reads the record again under the lock.
- Only System Managers with System User accounts operate these records.
- Secrets never go into an SSH Task. The Cargo installer is the only exception; the gateway daemon receives the public JWKS values instead.

## Proxy Server

- Atlas sends the new peer list to active nodes before it publishes a node in regional DNS.
- A job pushes a changed configuration digest every minute.

See the [HTTP proxy specification](../../services/http-proxy/SPEC.md).

## Cargo Server

- Provision needs an Active Proxy Server and no attached VM.
- The installer environment supplies every name in `ENROLMENT_VARS` of `atlas/scripts/install-cargo.sh`. A test compares the lists.
- The storage cluster request is `private/files/cargo-storage-cluster.json`. Installation reads it. Archive removes it.
- The Datum host request is `private/files/cargo-telemetry.json`. Installation writes it to the `default_telemetry_config` site config of Cargo. Archive removes it.
- Tokens, 365 days each: Atlas `atlas-admin:<region-id>` (subject `cargo`, scope `*`, tenant `0`) and proxy `atlas-proxy:<region-id>` (scope `site:*`, suffix `-svc`).
- Routes: `cargo` and `cargo-pilot` to the VM mesh address. Active after `/api/method/ping` returns `pong`.
- The bucket job uses a 5-minute `atlas-cargo:<region-id>` token. It never replaces configured object storage.

## IPv6 Router Server

- The pool needs a prefix of `/84` or shorter, no gateway, and no allocations.
- Installation fails if the eBPF program is not attached.
- Archive is refused while tenant allocations use the pool.

## WireGuard Gateway Server

- Creation needs a listen port next to the image and IPv4 allocation, plus an Active Proxy Server. It returns the daemon URL and the JWT audience for Central.
- Central calls the daemon inside the VM through the `<gateway>.<wildcard-domain>` proxy route with an Ed25519 JWT for the `atlas-wg-gateway:<region>` audience. The daemon owns the peer list; Atlas never syncs it.
- Archive removes the proxy route and terminates the VM; the peer list dies with it.

See the [WireGuard gateway specification](doctype/wireguard_gateway_server/SPEC.md).

## Related

- [IPv6 router service](../../docs/networking/ipv6-router.md)
- [HTTP proxy](../../docs/networking/http-proxy/index.md)
