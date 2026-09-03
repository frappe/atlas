# network: per-VM namespaced networking

[internal SPEC](../SPEC.md) · overview: [docs/networking.md](../../docs/networking.md)

## Purpose

Package `network` gives each virtual machine its own network namespace. The premise
is isolation: because each namespace is a private network stack, every VM reuses the
same fixed guest address, and the transit link is derived from the VM's unique uid.
So metald keeps no IP allocator and no shared network state. The namespace holds a
TAP that firecracker attaches to, plus a veth uplink to the host with source NAT.

## Terms

- `netns`: a Linux network namespace. It is a private network stack (interfaces,
  routes, iptables) isolated from the host and from other VMs.
- `TAP`: a virtual layer-2 device. firecracker attaches the guest NIC to it.
- `veth`: a virtual Ethernet pair. Two ends act as a cable. One end moves into the
  netns, the other stays on the host.
- `MASQUERADE`: source NAT that rewrites a packet's source to the outgoing
  interface's address.
- `/30`: a 4-address subnet, used here for one point-to-point veth link.

## Types

`Linux` is `struct{}`. It holds no state. Every field is derived from the VM id or
uid, so `Resolve` rebuilds a NIC without touching the system.

| Type | Role |
|---|---|
| `Allocator` | Interface: `Allocate`, `Resolve`, `Release`. |
| `Linux` | The one implementation. `NewLinux()`. |
| `Request` | `VMID`, `Ref` (unused until multiple networks exist), `UID`/`GID` (TAP owner). |
| `NIC` | `NetnsPath` (jailer `--netns`), `TapName` (firecracker `host_dev_name`), `MAC`, `GuestIP`, `GatewayIP`. |

## Naming and addressing

```text
netns:   metal-<vmID>            path /run/netns/metal-<vmID>
TAP:     tap0                    inside the netns, owned by uid:gid
veth:    vh-<uid> (host)  <->  vg-<uid> (moved into the netns)   fits the 15-char limit

guest:   172.16.0.2              fixed in every netns
gateway: 172.16.0.1/24           on tap0 inside the netns
MAC:     02:<5 bytes of sha256(vmID)>   stable, locally-administered unicast

transit /30 from uid:  base = 10.0.0.0 + uid*4
   vh-<uid> host side = base+1 /30
   vg-<uid> netns side = base+2 /30
   default route in the netns -> base+1
```

The guest IP repeats across VMs, which is safe because each netns is isolated. The
uid names the veth pair and the transit subnet, so two VMs never collide.

## Topology and packet path

```text
 [guest] --eth--> tap0 (gw 172.16.0.1) --[ netns metal-<vmID> ]-- vg-<uid> (base+2/30)
                                            default route via base+1
                                            POSTROUTING MASQUERADE -o vg-<uid>
                                                 |
                                               veth
                                                 |
 [host] vh-<uid> (base+1/30) --> uplink MASQUERADE (net-setup.sh) --> internet
```

NAT happens twice. Inside the netns, `MASQUERADE -o vg` rewrites the guest source to
the transit address. On the host, the uplink rule from `scripts/net-setup.sh`
rewrites the transit source to the uplink address. The guest reaches the metadata
service at `169.254.169.254`, which firecracker serves (see
[internal/firecracker/SPEC.md](../firecracker/SPEC.md)), not this package.

## Allocate

`Allocate` runs an ordered list of `ip`, `sysctl`, and `iptables` steps. On any
failure it rolls back the whole namespace.

```text
1  ip netns add metal-<id>
2  lo up
3  tuntap add tap0 mode tap user <uid> group <gid>
4  addr add 172.16.0.1/24 dev tap0
5  tap0 up
6  ip link add vh-<uid> type veth peer name vg-<uid>
7  move vg-<uid> into the netns
8  host: addr add base+1/30 dev vh-<uid> ; vh up
9  netns: addr add base+2/30 dev vg-<uid> ; vg up
10 netns: default route via base+1
11 netns: sysctl net.ipv4.ip_forward=1
12 netns: iptables -t nat -A POSTROUTING -o vg-<uid> -j MASQUERADE

on any failure -> Release (ip netns del) + ip link del vh-<uid>
```

## Resolve and Release

- `Resolve(vmID)` rebuilds the `NIC` from the id alone, with no system calls. It
  reconfigures a stopped VM whose netns still exists.
- `Release(vmID)` runs `ip netns del`, which removes the TAP and both veth ends.

## Prerequisite

The host must already have `ip_forward` on and an uplink `MASQUERADE` for the
transit range. `scripts/net-setup.sh` sets these once. metald does not.

## Design notes

Why the main choices were made.

- Namespace per VM. An isolated netns lets every guest use the same `172.16.0.2`, so
  nothing has to hand out a unique guest IP. It also confines each VM's routes,
  interfaces, and iptables, so one VM cannot see or disturb another's network.
- No IP allocator. The uid is already unique per VM, so the transit `/30` is computed
  as `10.0.0.0 + uid*4`. Deterministic addressing lets `Resolve` rebuild every field
  from the id alone, with no table to persist or lock.
- Deterministic MAC. The MAC is `02:` plus five bytes of `sha256(vmID)`, so it is
  stable across restarts and needs no storage. The `02` prefix marks it
  locally-administered, so it never clashes with a real vendor MAC.
- veth named by uid, not id. A Linux interface name is capped at 15 characters, and a
  VM id is a full UUID. `vh-<uid>` and `vg-<uid>` stay short and unique. The netns has
  no such limit, so it uses the full id (`metal-<uuid>`).
- NAT inside the namespace. `MASQUERADE` on the netns egress (`vg`) keeps the per-VM
  NAT rule inside that VM's namespace, so deleting the netns removes the rule too. The
  host adds a second `MASQUERADE` for the whole transit range on the uplink.
- Teardown by deleting the netns. `Release` runs `ip netns del`, which removes the
  TAP and both veth ends at once, so there is no per-interface cleanup to get wrong.

## Related

- [docs/networking.md](../../docs/networking.md) broad networking overview.
- [internal/firecracker/SPEC.md](../firecracker/SPEC.md) attaches the guest to `tap0` and serves MMDS.
- [internal/hostcmd/SPEC.md](../hostcmd/SPEC.md) runs the `ip`, `sysctl`, and `iptables` commands.
- [docs/host-layout.md](../../docs/host-layout.md) the netns and interface names on the host.
