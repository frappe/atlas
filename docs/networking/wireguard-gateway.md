# WireGuard gateway

The WireGuard gateway lets a customer device reach the private mesh addresses of its tenant's VMs. Each region runs a cluster of 1 to 5 gateway nodes. Every node keeps the complete device table. Any ready node can accept a device API request.

```text
Central ──POST /v1/peers──> wireguard.<wildcard>  (regional DNS)
                                  │ any node accepts the write; the cluster replicates it
                                  ▼
          wireguard-001 ⇄ wireguard-002 ⇄ wireguard-003
                                  │ each node serves only the devices assigned to it
device ──UDP──> wireguard-00N.<wildcard>:<listen port> ──mesh──> tenant VMs (fdaa:<region>:<tenant>::/64)
```

## Register a device

Central registers each device once and gives the returned settings to the customer. Atlas stores no devices.

```http
POST https://wireguard.par-1.example.com/v1/peers
Authorization: Bearer <JWT for atlas-wg-gateway:<region ID>>

{"tenant_id": 42, "client_id": 9, "public_key": "<device public key>"}
```

The answer holds the WireGuard settings of the device:

| Field | Example | Use |
| --- | --- | --- |
| `address` | `fdac:1:2:0:2a:0:9:0/128` | Interface address of the device. |
| `allowed_ips` | `["fdaa:1:0:2a::/64"]` | The VM addresses of the tenant. |
| `endpoint` | `wireguard-002.par-1.example.com:51820` | The node that serves the device. |
| `public_key` | `<node public key>` | Public key of that node. |

A repeated request with the same key returns the same settings. A new device goes to the node with the fewest devices. `DELETE /v1/peers/{tenant_id}/{client_id}` removes a device, and `GET /v1/peers` lists every device with its `node_id`.

`PUT /v1/peers` replaces the complete table, for example to restore it from Central's records. Send each device's `node_id` from the list, because the device address carries the node number. A device without `node_id` goes to the node with the fewest devices.

A JWT needs the scope `peers:update` to change devices or `peers:read` to list them. A token with a `tenant` claim is refused. The [control cluster](../../services/control-cluster/SPEC.md) validates tokens the same way as the HTTP proxy.

## Reach a VM

A VM accepts gateway traffic only when **Actions > Enable WireGuard Gateway Access** is on. Host sync adds each active node's return route to the VM namespace. The guest sends IPv6 traffic to the host, and the namespace selects the next hop.

If the VM has its own route to the same destination, Metal keeps that route. The custom route can prevent replies from reaching devices on that gateway node.

The gateway eBPF filters drop a packet unless the device and the VM belong to the same tenant and region. No address translation occurs, so the VM sees the device address.

## Addresses

```text
fdac | region 16 | node 16 | tenant 32 | client 32 | zero 16
```

Each node owns one `fdac:<region>:<node>::/48`. Atlas reserves `fdaa::/16`, `fdab::/16`, and `fdac::/16`. A customer network that uses one of these ranges conflicts with the routes of its devices, so it must use another range.

## Add a node

Atlas creates each node as a privileged tenant-0 VM with a public IPv4 address. It generates the node WireGuard key and sends it in `/etc/atlas/wireguard-gateway.toml` with the member list, the cluster password, the JWKS settings, and the wildcard certificate.

Atlas publishes `wireguard-NNN.<wildcard>`, installs the package, and configures the new node. Atlas then adds the node to the cluster and updates the other members. It waits for `/readyz` before it adds the node to the health-checked `wireguard.<wildcard>` name. A slow install does not add a voting member. A scheduled job checks for changed configurations each minute.

The firewall admits UDP on the listen port, TCP 443, and ICMP from any address. It also admits traffic from the region mesh.

## Rebuild or archive a node

If a node fails, its assigned devices disconnect. To replace its VM, select **Dangerous Actions > Rebuild** and a free tenant-0 IPv4 allocation. The gateway record keeps the VM image and shape, so Rebuild needs only a new address.

Archive and Rebuild remove the node from DNS and stop its API before they update the surviving cluster members. If SSH cannot stop the API, Atlas asks Metal to stop the VM and waits for confirmation. Atlas then updates the survivors and terminates the old VM. If an action fails, the record shows `Failed` and the action name. Run the action again after you correct the failure.

Rebuild keeps the node name, number, WireGuard key, and device assignments. The new node restores the device table from the other members. Devices reconnect after their node name points to the new address. A single-node cluster has no peer from which to restore its table. Restore it with `PUT /v1/peers`.

Before you archive a node, remove its devices with `DELETE /v1/peers/{tenant_id}/{client_id}`. Archive does not change the device table. A device of an archived node stays in the table with an empty `endpoint` and `public_key` until you remove it. A table restore keeps that device too.

## Check cluster health

Route 53 normally returns only nodes whose `/readyz` check passes. If all checks fail, Route 53 can return every unhealthy node. See [multivalue answer routing](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/routing-policy-multivalue.html). Devices keep using the node name. `/healthz` reports whether the node loaded its table and configured `wg0`.

The leader places a new device only on a node that reported a configured `wg0` in a recent heartbeat, so a node that is down or has a broken tunnel gets no new devices.

## Check a node

| Check | Command |
| --- | --- |
| WireGuard peers | `wg show wg0` |
| Gateway route | `ip -6 route show dev wg0` |
| Tenant filters | `tc filter show dev wg0 ingress` and `tc filter show dev eth0 ingress` |
| API and cluster | `systemctl status atlas-wg-gateway-api` and `curl -s https://wireguard-NNN.<wildcard>/readyz` |

::: details Source code and tests

- [Gateway record](../../atlas/service/doctype/wireguard_gateway_server/wireguard_gateway_server.py) and [provisioning](../../atlas/service/core/wg_gateway/provisioning.py).
- [Daemon](../../services/wg-gateway/daemon/gatewayd/main.py), [device table](../../services/wg-gateway/daemon/gatewayd/peers.py), and [eBPF filters](../../services/wg-gateway/bpf/gateway.c).
- [Daemon tests](../../services/wg-gateway/daemon/tests/test_peers.py) and [provisioning tests](../../atlas/service/core/wg_gateway/test_provisioning.py).

:::
