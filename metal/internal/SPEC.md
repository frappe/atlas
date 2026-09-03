# internal: metald packages

[metal SPEC](../SPEC.md) · overview: [docs/architecture.md](../docs/architecture.md)

## Purpose

`internal/` holds the packages that make up metald. This file is a router. The
premise is a clean layering: `vm` defines the contract, `firecracker` implements it
on top of the host packages, and `api` serves it. Only `cmd/metald` wires the
concretes together.

## Packages

| Package | Role |
|---|---|
| [vm](vm/SPEC.md) | The hypervisor-agnostic VM contract: interfaces and value types. |
| [firecracker](firecracker/SPEC.md) | Implements the contract on jailer + firecracker. |
| [firecracker/api](firecracker/api/SPEC.md) | REST client over each VM's socket. |
| [api](api/SPEC.md) | The HTTP server over the driver. |
| [storage](storage/SPEC.md) | ZFS-backed VM disks, snapshots, and images. |
| [network](network/SPEC.md) | Per-VM network namespace, TAP, and NAT. |
| [systemd](systemd/SPEC.md) | Unit control over D-Bus. |
| [idalloc](idalloc/SPEC.md) | Per-VM uid/gid allocation. |
| [hostcmd](hostcmd/SPEC.md) | Host command execution with folded errors. |

## Dependency graph

Arrows point from a user to what it uses. `vm` is the anchor: everything points at
it, and it imports nothing internal.

```text
        cmd/metald  (composition root, builds the concretes)
             |
             v
            api ───────────────> vm
             ^                     ^
             | injects             | implements
        firecracker ──────────────┘
             |  \  \  \
             |   \  \  └────────> firecracker/api
             |    \  └─────────> systemd
             |     └──────────> idalloc
             v                v
         storage          network
             \               /
              └──> hostcmd <─┘
```

`api` sees only `vm`. `firecracker` composes `storage`, `network`, `systemd`,
`idalloc`, and `firecracker/api`. `storage` and `network` shell out through
`hostcmd`. `systemd`, `idalloc`, `hostcmd`, and `firecracker/api` are leaves.

## Related

- [metal SPEC](../SPEC.md) the module root.
- [cmd/metald/SPEC.md](../cmd/metald/SPEC.md) the daemon that wires these together.
- [docs/architecture.md](../docs/architecture.md) the broad architecture.
