# Networking

[metal SPEC](../SPEC.md) · overview: [docs/architecture.md](architecture.md)

Each VM gets its own network namespace. The premise is isolation: because each
namespace is private, every VM reuses the same fixed guest address, and the transit
link is derived from the VM's unique uid. So metald keeps no IP allocator. Detail:
[internal/network/SPEC.md](../internal/network/SPEC.md).

## Topology

```text
 [guest] --eth--> tap0 (gw 172.16.0.1) --[ netns metal-<id> ]-- vg-<uid>
                                            veth
 [host]  vh-<uid> --> uplink NAT (net-setup.sh) --> internet
```

The guest always sees `172.16.0.2`. The netns is named `metal-<id>`; the veth pair is
`vh-<uid>` and `vg-<uid>`. Detail:
[internal/network/SPEC.md](../internal/network/SPEC.md).

## Addressing

- Guest IP and gateway are fixed, one per isolated namespace.
- The transit `/30` comes from the uid (`10.0.0.0 + uid*4`), so no table is needed.
- The MAC is derived from the VM id, so it is stable and needs no storage.

Detail: [internal/network/SPEC.md](../internal/network/SPEC.md).

## NAT

Traffic is source-NATed twice: once inside the namespace on egress, then on the host
uplink. The host prerequisite (`ip_forward` plus an uplink `MASQUERADE`) is set once
by `internal/vm/scripts/net-setup.sh`. Detail:
[internal/network/SPEC.md](../internal/network/SPEC.md).

## Metadata service

The guest reads ssh keys and a generation token from the metadata service at
`169.254.169.254`. firecracker serves it, not the network package. Detail:
[internal/firecracker/SPEC.md](../internal/firecracker/SPEC.md).

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

Detail: [internal/network/SPEC.md](../internal/network/SPEC.md).
