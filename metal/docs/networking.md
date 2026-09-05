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

The guest address is `172.16.0.2`. The gateway is `172.16.0.1`. The guest MAC address is `06:00:ac:10:00:02`. It encodes the guest IPv4 address. Each guest is alone in its namespace, so the fixed values cannot collide. A warm VM keeps the MAC of the snapshot it resumed from, so a fixed value keeps the reported MAC true.

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

The private filters use a lower `tc` priority than the public filter, so a private packet stops at the private policer. One `tc` priority holds one protocol, so the private IPv4 and IPv6 filters use separate priorities. The virtual Ethernet removal clears the traffic-control rules.

`uplink` and `mesh` have a veth pair, so both receive the private limit. A `mesh` VM has no internet path, so Metal keeps a public limit and does not apply it. A `none` VM has no veth pair and receives no limits. Metal keeps the requested values and applies them when the veth pair returns.

## Atlas WG Mesh

Each VM has a private IPv6 address in `fdaa::/16`. Atlas WG Mesh routes it between hosts. Metal registers the address when it creates the veth pair and unregisters it when it removes the pair.

```text
guest fdaa::x
    |
   tap0  fe80::1
    |
namespace metal-<id>          route fdaa::x/128 dev tap0
    |                         route fdaa::/16 via fe80::1 dev vg-<user-id>
 vg-<user-id>                 proxy NDP for fdaa::x
    |
 vh-<user-id>  fe80::1        Atlas WG Mesh vm_hook, TC ingress
    |
   wg0
```

Atlas WG Mesh assumes the VM is directly behind the interface that it hooks. Metal puts a network namespace between them, so the namespace forwards IPv6 and answers neighbour solicitations for the guest with proxy NDP. The host route from `vm add` is on-link on `vh-<user-id>`.

`metald` runs the `atlas-wg-mesh` CLI. On every start it runs `status` and configures the host when the CLI reports no configuration. It then replays the existing VM network configurations, so a reinstalled or reset host restores the VM registrations without an operator. Enable it with `wg_mesh.enabled`.

A VM learns its address from MMDS. `atlas-metadata.service` in the guest reads `meta-data/mesh-ipv6`, writes a systemd-networkd drop-in, and reloads networkd. A timer repeats the unit, because a warm snapshot resumes with new MMDS content and systemd does not start a unit again after a resume. A warm VM receives its own address within the timer period.

The MTU is 1380 on `vh-<user-id>` and `vg-<user-id>`. A mesh packet gains a 40-byte outer IPv6 header and must fit the 1420-byte WireGuard MTU. The guest keeps its 1500-byte TAP, and the namespace returns ICMPv6 Packet Too Big for a larger packet.

`egress: none` removes the veth pair, so it also removes the mesh registration.

## WireGuard peers

`POST /sync` supplies the complete managed WireGuard peer set. Metal applies the set to `wg0` and stores it in `wireguard-peers.json`.

The VM specification stores `wireguard_mesh_ipv6`. Metal validates it, registers it with Atlas WG Mesh, and publishes it to the guest through MMDS.

## Metadata service

Firecracker serves MMDS at `169.254.169.254`. The payload contains the instance ID, SSH keys, hostname, mesh address, and optional user data.

`PUT /vms/{id}/ssh-keys` replaces all SSH keys. Active VMs receive the new MMDS payload immediately.

## Design notes

- A namespace for each VM lets every guest use the same private IPv4 address.
- User-ID-derived veth names fit the Linux interface name limit.
- Deterministic addresses and MAC values need no mutable allocator state.
- Egress is one axis: internet reachability. Mesh reachability is always on for a VM with a veth pair.
- The veth pair is the private attachment, so `mesh` keeps it and only `none` removes it.
- A change between `uplink` and `mesh` keeps the interface, so it does not disturb the Atlas WG Mesh hook.
- `egress: none` isolates the whole VM.
- The mesh registration follows the veth pair, so one rule covers every egress mode.
- The namespace routes the mesh address, because Atlas WG Mesh hooks the host end of the veth.
- MMDS carries the mesh address, because the address is per VM and the image is shared.
- The guest reads MMDS on a timer, because a warm snapshot resumes with new MMDS content.
- Nothing per-VM is baked into the image, so one image serves a cold VM and a warm VM.
- Tagged public IPv4 rules permit exact cleanup for one VM.
- `/sync` replaces the complete managed WireGuard peer set.
