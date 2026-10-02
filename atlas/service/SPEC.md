# Service module specification

[Atlas app specification](../SPEC.md)

Behavior: [Service VMs](../../docs/region/service-vms.md). This module runs Atlas services on tenant-0 VMs. A service is not a tenant workload.

## Types

| Type | Owns |
|---|---|
| `ProxyServer` (DocType) | One proxy node and its VM, at most five active |
| `CargoServer` (Single) | The regional Cargo VM |
| `IPv6RouterServer` (DocType) | One router VM and its pool, `ipv6-router-NNN` |
| `WireguardGatewayServer` (DocType) | One gateway node, its VM, and its WireGuard key, `wireguard-NNN` |
| `service_package` | Publishes a package when its digest changes |
| `core/proxy`, `core/cargo`, `core/ipv6_router`, `core/wg_gateway` | Provisioning |
| `core/warpgate` | Warpgate client, target sync, host access grants, and the UI certificate. See [People access through Warpgate](../../docs/region/host-access.md#people-access-through-warpgate). |

## Shared rules

- Each VM is created through `VirtualMachineService` as a privileged tenant-0 VM. Proxy and WireGuard gateway VMs need a reserved tenant-0 IPv4 allocation. The proxy guest firewall admits TCP 80 and 443 from public addresses.
- Atlas reaches service SSH through the VM host. See [Atlas access to hosts](../../docs/region/host-access.md#ssh).
- A job requeues `Pending` records every minute.
- A failure sets `Failed` with the phase and message. Nothing replaces the VM automatically.
- A site file lock guards each record. Code reads the record again under the lock.
- Only System Managers with System User accounts operate these records.
- Secrets never go into an SSH Task. The Cargo installer is the only exception. Proxy and gateway configurations go over SSH directly.

## Proxy Server

- Atlas sends the new peer list to active nodes before it publishes a node in regional DNS.
- The apply command maps each peer name to its mesh address in `/etc/hosts`, so replication stays on the mesh with the same name and certificate.
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

## Wireguard Gateway Server

- Nodes form one regional cluster like the proxy. Each node keeps the device table; Atlas stores no devices.
- Creation needs a listen port, an image, and a tenant-0 IPv4 allocation. It returns the regional API URL for Central.

See the [Wireguard Gateway Server specification](doctype/wireguard_gateway_server/SPEC.md).

## Related

- [IPv6 router service](../../docs/networking/ipv6-router.md)
- [HTTP proxy](../../docs/networking/http-proxy/index.md)

## Warpgate

- Warpgate runs in the Atlas VM, not in a service VM. A tenant-0 VM cannot reach host `wg0`, and the host firewall admits SSH only from the Atlas address.
- `WarpgateClient` is the only code that calls the Warpgate admin API. The version is pinned in `scripts/install-warpgate.py`.
- The target description `Managed by Atlas: <Metal Server name>` marks Atlas targets. The sync never touches other targets.
- A renamed host renames its target and its `host:<title>` role. Grants point at the role ID, so they stay.
- Atlas stores no grants. Warpgate ends a grant at `expires_at`.
- `WarpgateTokenManager` keeps one token: the one whose ID is in `warpgate_api_token_id`. Each `install-warpgate.py` run issues a token, and `configure-atlas` stores it with its ID.
- The sync reports one failure message at most once an hour while it stays the same.
