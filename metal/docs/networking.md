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
    ├─ egress none: no host uplink
    └─ egress host: vg-<user-id> <-> vh-<user-id> -> host uplink
```

## Addressing

The guest address is `172.16.0.2`. The gateway is `172.16.0.1`. Metal derives the MAC address from the VM ID.

For host egress, Metal derives one transit `/30` from the VM user ID. This removes the need for a persisted address allocator.

## NAT

Host egress adds a default route and namespace NAT. Host setup adds uplink NAT for the transit range.

`Allocate` is idempotent when the namespace already exists. `Release` removes public IPv4 rules before it removes the namespace.

## Public IPv4

A public IPv4 address requires host egress. Metal adds these rules:

- Host DNAT from the public address to the namespace transit address.
- Host SNAT from the namespace transit address to the public address.
- Host forwarding rules for the VM.
- Namespace DNAT from the transit address to the guest address.

Each rule has the comment `metal-public-ipv4-<vm-id>`. This lets Metal remove only the rules for one VM.

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
- `egress: none` leaves the VM without a host uplink.
- Tagged public IPv4 rules permit exact cleanup for one VM.
- `/sync` replaces the complete managed WireGuard peer set.
