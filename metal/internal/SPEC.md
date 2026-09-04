# internal: metald packages

[metal SPEC](../SPEC.md) · overview: [docs/architecture.md](../docs/architecture.md)

## Purpose

`internal/` contains the Metal runtime packages. `cmd/metald` creates concrete services and injects them into consumers.

The `vm` package defines the contracts. The other packages implement or consume those contracts.

## Packages

| Package | Role |
|---|---|
| [vm](vm/SPEC.md) | VM interfaces and value types. |
| [firecracker](firecracker/SPEC.md) | Firecracker VM implementation and desired-state operations. |
| [firecracker/api](firecracker/api/SPEC.md) | Firecracker REST client over a Unix socket. |
| [api](api/SPEC.md) | Authenticated HTTP API. |
| [reconciler](../docs/architecture.md) | VM state, image cache, warm artifact, and staging cleanup loops. |
| [storage](storage/SPEC.md) | ZFS pool, VM disks, images, and snapshot staging. |
| [network](network/SPEC.md) | VM namespaces and managed WireGuard peers. |
| [systemd](systemd/SPEC.md) | Unit control through D-Bus. |
| [idalloc](idalloc/SPEC.md) | Host user ID allocation. |
| `hostcmd` | Host command execution. |

## Dependency graph

```text
cmd/metald
   ├─ api -> vm
   ├─ reconciler -> vm
   ├─ firecracker -> vm, storage, network, systemd, idalloc, firecracker/api
   ├─ storage -> vm, hostcmd
   └─ network -> vm, hostcmd
```

Consumer packages define their interfaces. The `vm` package contains the public VM driver and VM contracts.

## Related

- [metal SPEC](../SPEC.md) routes to concept documents.
- [cmd/metald/SPEC.md](../cmd/metald/SPEC.md) describes runtime construction.
- [docs/architecture.md](../docs/architecture.md) gives the broad architecture.
