# Networking

[metal SPEC](../SPEC.md) · detail: [internal/network/SPEC.md](../internal/network/SPEC.md)

Metal gives each virtual machine an isolated Linux network namespace. Every guest can use the same private IPv4 settings.

## Topology

```text
guest eth0
    |
   tap0  172.16.0.1/24
    |
network namespace metal-<id>
    |
    +- egress none:   no veth pair, no path out
    +- egress mesh:   vg-<user-id> <-> vh-<user-id>
    +- egress uplink: vg-<user-id> <-> vh-<user-id> -> host uplink
```

## Egress modes

Egress controls internet reachability. It does not control mesh reachability. The veth pair is the private network attachment. The default route and the namespace NAT are the internet path.

| Mode | veth pair | Default route and NAT | Public IPv4 | VM can reach |
|---|---|---|---|---|
| `uplink` | yes | yes | allowed | mesh peers and the internet |
| `mesh` | yes | no | rejected | mesh peers only |
| `none` | no | no | rejected | nothing |

A change between `uplink` and `mesh` keeps the veth pair. A change to `none` removes it.

## Addressing

The guest address is `172.16.0.2`. The gateway is `172.16.0.1`. Metal derives the MAC address from the VM ID.

For `uplink` and `mesh`, Metal derives one transit `/30` from the VM user ID. This removes the need for a persisted address allocator.

## NAT

`uplink` adds a default route and namespace NAT. Host setup adds uplink NAT for the transit range.

`Allocate` is idempotent when the namespace already exists. `Release` removes public IPv4 rules before it removes the namespace.

## Public IPv4

A public IPv4 address requires `uplink`. Metal rejects it for `mesh` and `none`. Metal adds these rules:

- Host DNAT from the public address to the namespace transit address.
- Host SNAT from the namespace transit address to the public address.
- Host forwarding rules for the VM.
- Namespace DNAT from the transit address to the guest address.

Each rule has the comment `metal-public-ipv4-<vm-id>`. This lets Metal remove only the rules for one VM.

## Throughput limits

The VM network configuration can set `private_network_throughput_mibps` and `public_network_throughput_mibps`. Each value applies in both directions. A value of `0` does not apply a limit.

Metal applies the limits inside the namespace, on `vg-<user-id>`. The host end `vh-<user-id>` belongs to Atlas WG Mesh, which attaches a terminating `direct-action` program to its `clsact` hook. Metal keeps the policers on the end that it owns, so neither component can stop the other. Private traffic uses the RFC 1918 ranges `10.0.0.0/8`, `172.16.0.0/12`, and `192.168.0.0/16`, and the IPv6 unique local range `fc00::/7`, which contains every mesh prefix. Public traffic uses the remaining IPv4 addresses.

The VM is inside the namespace, so `egress` carries traffic from the VM and the remote end is the destination. `ingress` carries traffic to the VM and the two are reversed.

The private filters use a lower `tc` priority than the public filter, so a private packet stops at the private policer. The virtual Ethernet removal clears the traffic-control rules.

`uplink` and `mesh` have a veth pair, so both receive the private limit. A `mesh` VM has no internet path, so Metal keeps a public limit and does not apply it. A `none` VM has no veth pair and receives no limits. Metal keeps the requested values and applies them when the veth pair returns.

## WireGuard peers

`POST /sync` supplies the complete managed WireGuard peer set. Metal applies the set to `wg0` and stores it in `wireguard-peers.json`.

The VM specification also stores `wireguard_mesh_ipv6`. Metal validates and returns this value. The current VM allocator does not configure it in the guest.

## Metadata service

Firecracker serves MMDS at `169.254.169.254`. The payload contains the instance ID, SSH keys, hostname, and optional user data.

`PUT /vms/{id}/ssh-keys` replaces all SSH keys. Active VMs receive the new MMDS payload immediately.

## Design notes

- A namespace for each VM lets every guest use the same private IPv4 address.
- User-ID-derived veth names fit the Linux interface name limit.
- Deterministic addresses and MAC values need no mutable allocator state.
- Egress is one axis: internet reachability. Mesh reachability is always on for a VM with a veth pair.
- The veth pair is the private attachment, so `mesh` keeps it and only `none` removes it.
- A change between `uplink` and `mesh` keeps the interface, so it does not disturb the Atlas WG Mesh hook.
- `egress: none` isolates the whole VM.
- Tagged public IPv4 rules permit exact cleanup for one VM.
- `/sync` replaces the complete managed WireGuard peer set.
