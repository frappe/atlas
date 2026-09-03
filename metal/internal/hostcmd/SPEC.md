# hostcmd: host command execution

[internal SPEC](../SPEC.md) · overview: [docs/architecture.md](../../docs/architecture.md)

## Purpose

Package `hostcmd` runs host CLI tools and folds their output into the returned
error. A failure carries the tool's own message. The storage and network drivers
use it to shell out.

## Functions

| Function | Returns | On failure |
|---|---|---|
| `Run(ctx, name, args...)` | `error` | The error wraps the combined stdout and stderr. |
| `Output(ctx, name, args...)` | `stdout string, error` | The error wraps stderr, so the caller can classify it. |

## Error folding

```text
Run(ctx, name, args...):
   exec name args   (combined output)
   ok   -> nil
   fail -> "name: <err>: <combined output>"

Output(ctx, name, args...):
   exec name args   (stdout, stderr split)
   ok   -> stdout, nil
   fail -> "", "name: <err>: <stderr>"
```

## Related

- [internal/storage/SPEC.md](../storage/SPEC.md) runs `zfs` and `cp` through it.
- [internal/network/SPEC.md](../network/SPEC.md) runs `ip`, `sysctl`, and `iptables` through it.
