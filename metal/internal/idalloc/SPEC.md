# idalloc: per-VM id allocation

[internal SPEC](../SPEC.md) · overview: [docs/architecture.md](../../docs/architecture.md)

## Purpose

Package `idalloc` supplies one host user ID for each virtual machine from `[100000, 165535]`. The user ID is also the group ID.

## Stateless allocation

`idalloc` keeps no table. The Firecracker driver scans persisted VM configurations before allocation. A mutex protects concurrent reservations.

```text
Create in internal/firecracker:
  lock allocationMutex
    used IDs = scan machines/<vm-id>/config.json
    user ID = Allocate(used IDs)
    write machines/<vm-id>/config.json
  unlock allocationMutex
```

## Related

- [internal/firecracker/SPEC.md](../firecracker/SPEC.md) the consumer and the `usedIDs` scan.
- [docs/host-layout.md](../../docs/host-layout.md) where per-VM configs live on disk.
