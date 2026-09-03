# idalloc: per-VM id allocation

[internal SPEC](../SPEC.md) · overview: [docs/architecture.md](../../docs/architecture.md)

## Purpose

Package `idalloc` hands out one id per virtual machine from the reserved subuid range
`[100000, 165535]`. One id is both uid and gid, so each VM gets a private group.

## Stateless allocation

`idalloc` keeps no table. `Allocate` rebuilds the used set by scanning the live VMs'
persisted configs, so metald stays stateless. A mutex plus a write-then-release makes
concurrent creates safe.

```text
Create (in internal/firecracker):
  lock d.mu
    used = scan machines/<uuid>/config.json of live VMs
    id   = Allocate(used)               lowest id not in use
    write machines/<uuid>/config.json   next Create sees the id as used
  unlock d.mu
```

## Related

- [internal/firecracker/SPEC.md](../firecracker/SPEC.md) the consumer and the `usedIDs` scan.
- [docs/host-layout.md](../../docs/host-layout.md) where per-VM configs live on disk.
