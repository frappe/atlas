# Atlas WG Mesh operations guide

The `atlas-wg-mesh` CLI configures local host and VM lifecycle state. It embeds the BPF object. It does not run as a daemon. Linux NDP discovers remote VM owners, Linux NUD maintains them, and BPF routes packets after the CLI exits.

## Contents

- [Atlas WG Mesh operations guide](#atlas-wg-mesh-operations-guide)
  - [Contents](#contents)
  - [Requirements](#requirements)
  - [Network and security](#network-and-security)
  - [Build a release](#build-a-release)
  - [Install a host](#install-a-host)
  - [Add a VM](#add-a-vm)
    - [VMs behind a router](#vms-behind-a-router)
  - [Move a VM](#move-a-vm)
  - [Remove a VM](#remove-a-vm)
  - [List local VM ownership](#list-local-vm-ownership)
  - [Manage privileged VMs](#manage-privileged-vms)
  - [Rejoin after a dead declaration](#rejoin-after-a-dead-declaration)
  - [Check status](#check-status)
  - [Debug in production](#debug-in-production)
  - [Upgrade BPF programs](#upgrade-bpf-programs)
  - [Remove remote entries](#remove-remote-entries)
  - [Remove a host installation](#remove-a-host-installation)
  - [Force reset fallback](#force-reset-fallback)

## Requirements

Run the CLI as root. Configure WireGuard before you install Atlas WG Mesh. Give each host a global `fdab::/16` address on its WireGuard interface. Use a prefix that covers every other host, because the VM hook returns the tunnel packet to Linux routing and the host must have a route to the remote host address. Add a WireGuard peer for every other host. Set each peer `AllowedIPs` value to that peer's `/128` address.

Build and load the Atlas neighbour kernel module on every host before you configure Atlas WG Mesh. The NDP and NUD hooks call its kfuncs, so the BPF object cannot load without it:

```sh
atlas-wg-mesh module install
```

The command writes the embedded module sources to `/usr/src`, builds them against the running kernel headers, links the module into `/lib/modules`, and loads it. The build takes the kernel BTF from `/sys/kernel/btf/vmlinux`, so the module carries the BTF section that the BPF object needs. The host needs the `build-essential`, `dwarves`, and `linux-headers-$(uname -r)` packages. The metald install script runs the command automatically.

Run the command again after a kernel upgrade, because a module of one kernel release cannot serve another.

Atlas WG Mesh pins state at `/sys/fs/bpf/atlas-wg-mesh`.

## Network and security

| Use                        | Value                        |
| -------------------------- | ---------------------------- |
| VM address range           | `fdaa::/16`                  |
| WireGuard address range    | `fdab::/16`                  |
| Shared VLAN route          | `fdaa::/16 dev <uplink>`     |
| Uplink MTU                 | 1500 or greater              |
| WireGuard MTU              | Uplink MTU minus 60          |
| VM interface and guest MTU | 1380                         |

Discovery uses IPv6 NDP on the shared VLAN. Neighbour advertisements are not authenticated, so restrict the VLAN to trusted participating hosts. Cross-tenant traffic is permitted only when one endpoint is a controller-whitelisted privileged tenant-`0` VM address, so reserve those addresses for trusted platform services.

Hosts without a shared Layer-2 domain use [unicast NDP transport](unicast-network.md) over a routed IPv4 underlay instead.

## Build a release

Run this command on a Linux build host with Go and clang:

```sh
make build
```

Set a release version with `make build VERSION=v1.2.3`.

The command creates these self-contained binaries:

```text
dist/atlas-wg-mesh-linux-amd64
dist/atlas-wg-mesh-linux-arm64
```

Each binary embeds the BPF object. Copy the binary that matches the host CPU architecture. The host does not need `clang`, `bpftool`, or a separate BPF object file.

Use `make bpf` to build only the embedded BPF object. Use `make module` to build the kernel module. Use `make clean` to remove generated BPF and release files.

## Install a host

Run this command once on each host:

```sh
atlas-wg-mesh configure --uplink eno1.1680 --wireguard wg0
```

Name the shared VLAN interface itself, never its parent. The command mounts BPF file storage when necessary, enables IPv6 forwarding, enables proxy NDP on the uplink, installs the `fdaa::/16` VLAN route, writes the host configuration, pins BPF state, and attaches the NDP hook to both TC directions of the uplink and the WireGuard hook to `wg0`.

The VLAN route is not a WireGuard route. It lets route lookup for a VM destination select the VLAN, so Linux runs NDP on that link. Local VM delivery always uses the more specific host route on the VM interface.

## Add a VM

Create the VM interface with your virtualization system. Then register the VM address on the host:

```sh
atlas-wg-mesh vm add --interface veth0 --address fdaa:1:0:7::1 --mtu 1380
```

The command configures the host interface, adds the VM to the local BPF map, attaches the VM hook, and adds a proxy NDP entry for the VM address on the uplink. The host then answers neighbour solicitations for the VM on the shared VLAN.

`--mtu` defaults to `1380`. Set the guest address to `fdaa:1:0:7::1/128`, gateway to `fe80::1`, and MTU to the same value. Do not exceed the WireGuard MTU minus 40 bytes.

### VMs behind a router

`vm add` adds an on-link host route for the VM address on `--interface`. When the VM is not directly behind that interface, for example a Metal VM in its own network namespace, the router between them must forward IPv6, hold a route to the VM address, and answer neighbour solicitations for it with proxy NDP.

## Move a VM

Stop the VM on the old host. Remove it from the old host. Add it on the new host. Then start the VM on the new host.

```sh
atlas-wg-mesh vm remove --interface veth0 --address fdaa:1:0:7::1
atlas-wg-mesh vm add --interface veth0 --address fdaa:1:0:7::1
```

The first neighbour solicitation after the move reaches the new host, which answers with its own WireGuard address in the Atlas option. Senders update the learned location. The VM keeps its private IPv6 address, and no WireGuard change is required.

## Remove a VM

Run this command before you delete the VM interface:

```sh
atlas-wg-mesh vm remove --interface veth0 --address fdaa:1:0:7::1
```

The command removes the VM hook, the local BPF map entry, the host route, and the proxy NDP entry. It does not change privileged-VM policy: when an address is retired or reassigned, the controller must remove it from the privileged VM whitelist separately.

## List local VM ownership

Use this command to reconcile a host with the controller:

```sh
atlas-wg-mesh vm list
atlas-wg-mesh vm list --json
```

Each line contains the VM address and the owning host interface:

```text
fdaa:1:0:7::1	veth0
```

`--json` returns an array of objects with `address` and `interface` fields for controller automation.

An `ifindex:N` value means the registered interface no longer exists.

To clear that single orphaned ownership entry, run `vm remove` with the missing interface name and VM address. The command removes the map entry without resetting the rest of the host.

## Manage privileged VMs

Tenant-`0` is the privileged tenant. A privileged VM can communicate with other tenants only after the controller adds its full IPv6 address to the whitelist on each host. Other tenants can communicate only with those whitelisted privileged VMs, which preserves request and response traffic without exposing every tenant-`0` VM:

```sh
atlas-wg-mesh privileged-vm add --address fdaa:1:0:0::1
atlas-wg-mesh privileged-vm remove --address fdaa:1:0:0::1
atlas-wg-mesh privileged-vm list
atlas-wg-mesh privileged-vm list --json
```

`list --json` returns an array of objects with an `address` field, matching the shape used by `vm list --json`. The controller owns reconciliation: compare those addresses with the desired privileged-tenant VM addresses, and use `add` and `remove` to apply the difference. The whitelist is BPF state shared by all local hooks, so its desired contents must be synced to every Atlas WG Mesh host.

## Rejoin after a dead declaration

This is the normal controller rejoin path. It preserves the uplink and WireGuard hooks, healthy VM registrations, and learned remote locations:

1. Run `atlas-wg-mesh vm list --json`.
2. Compare the result with the controller's current desired state.
3. Remove addresses that the controller no longer assigns to this host.
4. Add addresses assigned to this host but missing from the list.

For example, remove one stale ownership entry without interrupting other VMs:

```sh
atlas-wg-mesh vm remove --interface fc-zomb --address fdaa:1:0:2::20
```

Reconcile immediately when the host reconnects. Until stale entries are removed, the host still answers neighbour solicitations for them. Do not replay a stale local manifest at boot; always use the controller's current desired state.

## Check status

Run this command to show the host configuration, local VM count, and privileged-VM count:

```sh
atlas-wg-mesh status
```


## Debug in production

Read [Debug in production](debug-in-production.md).

## Upgrade BPF programs

Run the newer CLI binary on the host:

```sh
atlas-wg-mesh upgrade
```

It compares the embedded BPF hash, keeps compatible pinned maps including `privileged_tenant_allowed_addresses`, and replaces every Atlas WG Mesh hook. If the embedded BPF hash differs and maps are incompatible, run `atlas-wg-mesh upgrade --force`; it rebuilds BPF state, restores local VMs from their routes, and clears learned remote locations. When the hashes already match, `upgrade --force` does nothing.

Use `atlas-wg-mesh version` to show CLI and BPF hashes.

## Remove remote entries

When a host is permanently unavailable, clear learned entries for its WireGuard address:

```sh
atlas-wg-mesh remote purge --host fdab::10
```

## Remove a host installation

Remove every VM from the host first. Then remove the host hooks, the VLAN route, and the pinned BPF state:

```sh
atlas-wg-mesh reset
```

## Force reset fallback

Use `reset --force` only when surgical reconciliation is not safe: for example, the pinned configuration cannot identify its uplink after a NIC change, the controller cannot establish desired state, or an operator needs a clean slate. It detaches every still-present VM hook, then clears local VM ownership and all pinned BPF state:

```sh
atlas-wg-mesh reset --force
```

Reconfigure the host and re-register only the VMs in the controller's current desired state:

```sh
atlas-wg-mesh configure --uplink eth0 --wireguard wg0
atlas-wg-mesh vm add --interface veth0 --address fdaa:1:0:7::1 --mtu 1380
```

A forced reset also clears `remote_vms` and `privileged_tenant_allowed_addresses`; NDP rebuilds remote locations, while the controller must reconcile privileged VMs again.
