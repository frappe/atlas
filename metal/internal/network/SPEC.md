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
| `Allocator` | Allocates, resolves, and releases one VM network. |
| `LinuxAllocator` | Linux namespace, TAP, veth, route, and NAT implementation. |
| `Request` | VM ID, egress mode, public IPv4, user ID, and group ID. |
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
guest -> tap0 -> namespace -> optional guest veth -> host veth -> uplink
```

Host egress uses namespace NAT and the host uplink NAT rule. Public IPv4 uses tagged DNAT, SNAT, and forwarding rules.

## Allocate

Metal always creates the namespace, loopback, TAP, and gateway. Host egress also creates the veth pair, transit route, forwarding, and NAT.

A public IPv4 address requires host egress. Metal adds tagged host and namespace forwarding rules.

Allocation returns existing deterministic settings when the namespace already exists. Release removes tagged public IPv4 rules and then removes the namespace.

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
