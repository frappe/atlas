# Networking

For Go code, follow the repository [Go anti-pattern rules](../../llm/go-code-review-guide.md).

[metal SPEC](../SPEC.md) · detail: [internal/network/SPEC.md](../internal/network/SPEC.md)

Every virtual machine gets the same private addresses. That is safe because each VM owns its own Linux network namespace, so nothing has to be allocated per VM and nothing has to be remembered between restarts.

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

The guest address is `172.16.0.2`, the gateway `172.16.0.1`, and the guest MAC `06:00:ac:10:00:02`, which encodes that address. A warm VM keeps the MAC of the snapshot it resumed from, so a fixed value keeps the reported MAC true.

For `uplink` and `mesh`, Metal derives one transit `/30` from the VM user ID, which removes the need for a persisted address allocator.

## Egress modes

Egress controls internet reachability. It does not control mesh reachability. The veth pair is the private network attachment.

| Mode | veth pair | Default route and NAT | Public IPv4 | VM can reach |
|---|---|---|---|---|
| `uplink` | yes | yes | allowed | mesh peers and the internet |
| `mesh` | yes | no | rejected | mesh peers only |
| `none` | no | no | rejected | nothing |

A change between `uplink` and `mesh` keeps the veth pair. A change to `none` removes it, and the mesh registration with it.

## Throughput limits

The VM configuration can set `private_network_throughput_mibps` and `public_network_throughput_mibps`. Each applies in both directions, and `0` means unlimited.

Private traffic uses the private IPv4 ranges and the mesh IPv6 range. Public traffic uses the remaining IPv4 addresses. The exact filters: [internal/network/SPEC.md](../internal/network/SPEC.md).

A `mesh` VM has no internet path, so Metal keeps a public limit and does not apply it. A `none` VM has no veth pair and receives no limits. The requested values are kept and applied when the veth pair returns.

## Firewall

The VM firewall filters public and mesh traffic in the VM network namespace. A disabled firewall permits all traffic. Rules stay stored while the firewall is disabled.

An enabled firewall permits established and related connections. Allow rules then permit new inbound or outbound traffic. An empty rule list blocks new traffic in that direction.

Rules support `any`, `tcp`, `udp`, and `icmp`. TCP and UDP rules can select one destination port or one inclusive range. Each rule has one or more canonical IPv4 or IPv6 prefixes. One firewall can have at most 50 prefix entries across both directions.

Metal inspects changed firewalls on the next reconcile pass. It also audits unchanged filter tables once per minute and replaces external changes.

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

Atlas WG Mesh also adds a proxy NDP entry for the guest address on the shared VLAN, so other hosts resolve the guest through the hosting node. Remote hosts learn the guest location from the NDP advertisement, which carries the owning host's `fdab::/16` address in the Atlas option. No Atlas discovery daemon runs, and the `atlas_neigh` kernel module must be loaded before the CLI configures a host.

`metald` runs the `atlas-wg-mesh` CLI. On every start it runs `status` and configures the host when the CLI reports no configuration. It then replays the existing VM network configurations, so a reinstalled or reset host restores the VM registrations without an operator.

Tenant 0 is the privileged tenant. A tenant-0 VM crosses tenants only when its address is in the Atlas WG Mesh whitelist, which `POST /v1/sync` carries in full.

A VM learns its address from MMDS. `atlas-metadata.service` reads `meta-data/mesh-ipv6` every 250 ms and writes a systemd-networkd drop-in. This updates the address after a warm snapshot resumes. The service then resets the guest resolver and time sync, because both resume backed off. See [Per-VM metadata](../../atlas/vm/SPEC.md#per-vm-metadata).

Namespace routing, proxy NDP, MTU, public IPv4 rules, and filter placement: [internal/network/SPEC.md](../internal/network/SPEC.md).

## Packet activity and wake

Metal tracks TCP traffic from the host to the guest. It ignores other packets and all guest-to-host traffic. This prevents ARP and IPv6 housekeeping from keeping a VM awake.

```text
host TCP packet -> tap0 egress TCX hook -> record activity
                                      |
                                      +-> VM is armed -> send wake event -> restore VM
```

The eBPF program observes packets but does not change them. It stores the last packet time by VM user ID.

The first packet can be lost while Firecracker starts. Clients must retry. A metald restart creates a new activity baseline. This can delay sleep, but it cannot make a VM sleep early. See [internal/network/SPEC.md](../internal/network/SPEC.md).

## WireGuard peers

`POST /v1/sync` supplies the complete managed peer set. Metal applies it to `wg0` and records what it applied, so it never disturbs peers added by other tools.

Each peer endpoint is a public address, so the tunnel crosses the uplink. Atlas WG Mesh discovery uses the private network. The WireGuard MTU follows the uplink MTU for this reason.

The region prefix on `wg0` makes every peer on-link. Runtime `wg set` installs no system routes, so the manager additionally owns one `/128` route per peer into `wg0`. A peer that leaves the set loses its peer entry and its route; a missing route is reinstalled on the next apply, because routes do not survive a reboot while the managed peer state does.

## Design notes

- A namespace for each VM lets every guest use the same private IPv4 address.
- Deterministic addresses, MAC values, and veth names need no mutable allocator state.
- User-ID-derived veth names fit the Linux interface name limit.
- Egress is one axis: internet reachability. Mesh reachability follows the veth pair.
- A change between `uplink` and `mesh` keeps the interface, so it does not disturb the Atlas WG Mesh hook.
- The namespace routes the mesh address, because Atlas WG Mesh hooks the host end of the veth.
- MMDS carries the mesh address, because the address is per VM and the image is shared.
- Nothing per VM is baked into the image, so one image serves a cold VM and a warm VM.
- Tagged public IPv4 rules permit exact cleanup for one VM.
