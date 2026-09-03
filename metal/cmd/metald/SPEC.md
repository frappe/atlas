# metald: the daemon entrypoint

[metal SPEC](../../SPEC.md) · overview: [docs/architecture.md](../../docs/architecture.md)

## Purpose

Command `metald` is the daemon binary. The premise is that it is the composition
root: the one place that constructs the concrete implementations and injects them.
It parses the config, builds the driver stack, and serves the HTTP API over TCP or a
Unix socket.

## Command

```text
metald serve [--config path]     run the server (serve is the default)
```

`--config` defaults to `/var/lib/metal/metald.toml`. A missing default file is fine;
a missing explicit path is an error.

## Types

| Type | Role |
|---|---|
| `opts` | The resolved config: `firecracker.Config`, `pool`, `kernelDir`, `imagesDir`, `listen`, `baseDir`. |
| `fileConfig` | The optional TOML overlay. Sections group keys by owner: `[metald]`, `[firecracker]`, `[jailer]`, `[zfs]`. |
| `deriveDirs` | Places `machines`, `kernels`, `images` under `baseDir`. |

## Startup wiring

The composition root. Only `main` builds concretes. Every package below takes an
interface.

```text
main -> load(config) -> serve:
  makeDirs   MachinesDir 0750, SocketsDir 0700, kernelDir 0755, imagesDir 0755
  systemd.Connect                                   -> units
  driver = firecracker.New(cfg, units,
                           storage.NewZFS(pool, kernelDir, imagesDir),
                           network.NewLinux())
  e = api.New(driver)
  ln = listen(addr)        tcp host:port, or unix:/path (stale unlinked, chmod 0660)
  e.Start
```

## Config keys

| Key | Default | Meaning |
|---|---|---|
| `metald.base_dir` | `/var/lib/metal` | Holds `machines`, `kernels`, `images`. |
| `metald.listen` | `127.0.0.1:8080` | API address: `host:port` or `unix:/path`. |
| `firecracker.binary_path` | `/usr/bin/firecracker` | firecracker binary. |
| `firecracker.sockets_dir` | `/run/metal` | Short API socket symlinks. |
| `jailer.binary_path` | `/usr/bin/jailer` | jailer binary. |
| `zfs.pool` | `metal` | ZFS pool name. |

See `config.example.toml`. Full config and dev-host setup: [docs/testing.md](../../docs/testing.md).

## Related

- [docs/architecture.md](../../docs/architecture.md) the startup graph and request path.
- [internal/api/SPEC.md](../../internal/api/SPEC.md) the server it starts.
- [internal/firecracker/SPEC.md](../../internal/firecracker/SPEC.md) the driver it builds.
- [docs/testing.md](../../docs/testing.md) config keys and the dev host.
- [docs/host-layout.md](../../docs/host-layout.md) the on-disk layout under `base_dir`.
