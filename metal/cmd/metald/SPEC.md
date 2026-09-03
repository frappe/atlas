# metald: the daemon entrypoint

[metal SPEC](../../SPEC.md) · overview: [docs/architecture.md](../../docs/architecture.md)

## Purpose

Command `metald` loads host configuration, creates runtime services, and starts the HTTP server. It is the composition root.

## Command

```text
metald serve [--config path]
```

The default configuration path is `/var/lib/metal/metald.toml`. A missing default file is permitted.

## Types

| Type | Role |
|---|---|
| `opts` | Resolved Firecracker, ZFS, listener, authentication, and base-directory settings. |
| `fileConfig` | TOML sections for metald, Firecracker, Jailer, and ZFS. |

## Startup wiring

```text
load configuration
   -> create required directories
   -> connect to systemd
   -> create storage stores
   -> create the WireGuard manager
   -> create the Firecracker driver
   -> start the VM and image reconcilers
   -> create the authenticated API
   -> listen and serve
```

The storage constructor is `storage.NewStores(pool, imagesDirectory)`. It returns the pool, VM, image, and snapshot stores.

The network constructor is `network.NewLinuxAllocator()`. The Firecracker driver receives separate VM, image, and snapshot dependencies.

## Config keys

| Key | Default | Meaning |
|---|---|---|
| `metald.base_dir` | `/var/lib/metal` | Stores machines, images, policies, peers, and staging files. |
| `metald.listen` | `127.0.0.1:8080` | TCP address or `unix:/path`. |
| `metald.auth_token_hash` | none | Required lowercase SHA-256 token digest. |
| `firecracker.binary_path` | `/usr/bin/firecracker` | Firecracker binary. |
| `firecracker.sockets_dir` | `/run/metal` | Short API socket links. |
| `jailer.binary_path` | `/usr/bin/jailer` | Jailer binary. |
| `zfs.pool` | `metal` | ZFS pool name. |

See `config.example.toml` for the complete file format.

## Runtime loops

The VM reconciler processes desired VM states. The image reconciler downloads cached images, creates warm artifacts, and removes idle local data.

## Related

- [docs/architecture.md](../../docs/architecture.md) describes the runtime graph.
- [internal/api/SPEC.md](../../internal/api/SPEC.md) describes the server.
- [internal/firecracker/SPEC.md](../../internal/firecracker/SPEC.md) describes the driver.
- [docs/testing.md](../../docs/testing.md) describes host setup.
