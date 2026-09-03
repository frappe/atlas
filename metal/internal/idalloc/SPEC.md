# idalloc: per-VM id allocation

[internal SPEC](../SPEC.md) · overview: [docs/architecture.md](../../docs/architecture.md)

## Purpose

Package `idalloc` hands out one id per virtual machine from a reserved range. It holds no state.
The caller passes the used ids, so metald stays stateless.

## Types

| Type | Role |
|---|---|
| `Range{Min, Max uint32}` | Inclusive id range. |
| `DefaultRange` | `{100000, 165535}`, the classic subuid range (65536 ids). |
| `ErrExhausted` | The range is full. |
| `Range.Allocate(used map[uint32]bool)` | Returns the lowest id in the range not in `used`. |

## Id model

One id serves as both uid and gid, so each VM gets a private group.

```text
reserved range  [100000 .. 165535]   65536 ids
      |
      | Allocate(used) -> lowest id not in used
      v
   one id per VM  =>  uid == gid   (private per-VM group)
      used by jailer (--uid --gid) and as the network uid
```

The uid also names the veth pair and the transit subnet. See
[internal/network/SPEC.md](../network/SPEC.md).

## Stateless allocation

`idalloc` keeps no table. The used set is rebuilt on each `Create` by scanning the
live VMs' persisted configs. A mutex plus a write-then-release makes it safe for
concurrent creates.

```text
Create (in internal/firecracker):
  lock d.mu
    used = scan machines/<uuid>/config.json of live VMs   (state lives on disk)
    id   = Range.Allocate(used)
    write machines/<uuid>/config.json with id             (next Create sees it used)
  unlock d.mu
```

## Related

- [internal/firecracker/SPEC.md](../firecracker/SPEC.md) the consumer and the `usedIDs` scan.
- [docs/host-layout.md](../../docs/host-layout.md) where per-VM configs live on disk.
