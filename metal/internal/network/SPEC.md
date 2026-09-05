# network: VM and host networking

[internal SPEC](../SPEC.md) · overview: [docs/networking.md](../../docs/networking.md)

## Purpose

Package `network` creates isolated VM networks and manages host WireGuard peers.

## Terms

- A network namespace is one isolated Linux network stack.
- A TAP connects Firecracker to the guest network.
- A veth pair connects a namespace to the host.
- MASQUERADE provides source NAT.

## Types

| Type | Role |
|---|---|
| `Allocator` | Allocates, updates, resolves, and releases one VM network. |
| `LinuxAllocator` | Linux namespace, TAP, veth, route, and NAT implementation. |
| `Request` | VM ID, egress mode, public IPv4, throughput limits, user ID, and group ID. |
| `Egress` | Internet reachability mode: `uplink`, `mesh`, or `none`. |
| `Interface` | Namespace path, TAP name, MAC address, guest address, and gateway. |

`NewLinuxAllocator()` returns the concrete allocator. It keeps no mutable state.

## Naming and addressing

```text
namespace     metal-<vm-id>
TAP           tap0
guest IPv4    172.16.0.2
gateway       172.16.0.1/24
host veth     vh-<user-id>
guest veth    vg-<user-id>
```

The MAC address is `02:` plus five bytes from the VM ID SHA-256 value. The transit `/30` derives from the user ID.

## Topology and packet path

```text
guest -> tap0 -> namespace -> guest veth -> host veth -> uplink
                              \___________________/     \______/
                               uplink and mesh            uplink
```

`uplink` uses namespace NAT and the host uplink NAT rule. Public IPv4 uses tagged DNAT, SNAT, and forwarding rules.

## Egress modes

`Egress` controls internet reachability only. The veth pair is the private network attachment.

| Mode | veth pair | Default route and NAT | Public IPv4 |
|---|---|---|---|
| `uplink` | yes | yes | allowed |
| `mesh` | yes | no | rejected |
| `none` | no | no | rejected |

`host` is a deprecated alias for `uplink`. An empty value reads as `uplink`. The API requires the field.

## Allocate

Metal always creates the namespace, loopback, TAP, and gateway. `uplink` and `mesh` also create the veth pair and the transit addresses. `uplink` also creates the default route, forwarding, and NAT.

A public IPv4 address requires `uplink`. Metal adds tagged host and namespace forwarding rules.

Allocation returns existing deterministic settings when the namespace already exists. Release removes tagged public IPv4 rules and then removes the namespace.

## Update

`Update` reconciles to the desired state. It does not enumerate the transitions between modes.

```text
                      veth pair      internet path    public IPv4
desired uplink   ->   present        present          optional
desired mesh     ->   present        absent           absent
desired none     ->   absent         absent           absent
```

Each row is one add or remove against the current host state. `uplink <-> mesh` keeps the veth pair, so it does not disturb the Atlas WG Mesh hook on `vh-<user-id>`. A change to or from `none` adds or removes the interface.

## Traffic control

`Request` supports private and public limits in Mbps. `0` leaves a traffic type unlimited. Metal applies `tc` policers to `vg-<user-id>`. Private traffic uses RFC 1918 and `fc00::/7`. Public traffic uses the remaining IPv4 addresses. `Update` changes the limits, egress, and public IPv4 rules without a restart.

Atlas WG Mesh owns `vh-<user-id>`. Metal owns `vg-<user-id>` and keeps the limits there. `uplink` and `mesh` have a veth pair and receive the limits. A `mesh` VM has no internet path, so Metal rejects a public limit for it. `egress: none` applies no limits. Metal keeps the values and applies them when the veth pair returns.

## Resolve and Release

`Resolve` returns deterministic guest network settings without a host command. `Release` removes public IPv4 rules and the namespace.

## Prerequisite

Host setup must enable IPv4 forwarding and uplink NAT. Metal does not select or configure the host uplink.

## WireGuard

`WireGuardManager` applies the complete controller peer set to `wg0`. It stores managed peer state in `wireguard-peers.json`.

The manager validates peer names, node IDs, public keys, endpoints, and duplicate values.

## Related

- [docs/networking.md](../../docs/networking.md) gives the broad network design.
- [internal/firecracker/SPEC.md](../firecracker/SPEC.md) attaches Firecracker to the TAP.
- [docs/host-layout.md](../../docs/host-layout.md) lists host network paths.
