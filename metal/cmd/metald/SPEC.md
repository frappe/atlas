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
| `opts` | Resolved Firecracker, ZFS, WireGuard, Atlas WG Mesh, listener, authentication, and base-directory settings. |
| `fileConfig` | TOML sections for metald, Firecracker, Jailer, ZFS, WireGuard, and Atlas WG Mesh. |

## Startup wiring

```text
load configuration
   -> create required directories
   -> connect to systemd
   -> create storage stores
   -> create the WireGuard manager
   -> connect Atlas WG Mesh and configure the host
   -> create the Firecracker driver
   -> restore VM mesh registrations
   -> start the VM and image reconcilers
   -> create the authenticated API
   -> listen and serve
```

The storage constructor is `storage.NewStores(pool, imagesDirectory)`. It returns the pool, VM, image, and snapshot stores.

The network constructor is `network.NewLinuxAllocator(mesh)`. A `nil` mesh leaves VMs off Atlas WG Mesh. The Firecracker driver receives separate VM, image, and snapshot dependencies.

`connectMesh` runs on every start. It runs `atlas-wg-mesh status`, and configures the host when the CLI reports no configuration. `RestoreNetworks` then replays each non-destroyed VM network, so a reinstalled or reset host recovers its VM mesh registrations without an operator.

`wg_mesh.uplink` has no default. The Atlas WG Mesh uplink hook consumes discovery traffic for every VLAN under the interface it attaches to, so a parent interface silently blackholes discovery for its own VLANs. Only the controller knows which interface carries discovery, so metald requires the name.

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
| `wireguard.interface` | `wg0` | Underlay interface for managed peers and Atlas WG Mesh. |
| `wg_mesh.enabled` | `false` | Register VM mesh addresses with Atlas WG Mesh. |
| `wg_mesh.binary_path` | `/usr/local/bin/atlas-wg-mesh` | Atlas WG Mesh CLI. |
| `wg_mesh.uplink` | none | Discovery uplink. Required when the mesh is enabled. |

See `config.example.toml` for the complete file format.

## Runtime loops

The VM reconciler processes desired VM states. The image reconciler downloads cached images, creates warm artifacts, and removes idle local data.

## Related

- [docs/architecture.md](../../docs/architecture.md) describes the runtime graph.
- [internal/api/SPEC.md](../../internal/api/SPEC.md) describes the server.
- [internal/firecracker/SPEC.md](../../internal/firecracker/SPEC.md) describes the driver.
- [docs/testing.md](../../docs/testing.md) describes host setup.
