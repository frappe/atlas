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
