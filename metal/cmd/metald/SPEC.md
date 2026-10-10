# metald: the daemon entrypoint

For Go code, follow the repository [Go anti-pattern rules](../../../llm/go-code-review-guide.md).

[Metal specification](../../SPEC.md) · Behavior: [Metal daemon and API](../../../docs/region/metald.md)

## Purpose

Command `metald` is the composition root. It loads configuration, creates services, and starts the HTTPS servers.

## Command

```text
metald serve [--config path]
```

The default path is `/var/lib/metal/metald.toml`. A missing default file is permitted.

## Types

| Type | Role |
|---|---|
| `options` | Resolved runtime settings. |
| `fileConfig` | TOML file sections. |
| `tlsConfigurations` | TLS for both servers and the node client. |

## Startup wiring

`main.go` starts services in this order:

1. Configuration, TLS credentials, and directories.
2. systemd, storage stores, and the WireGuard manager.
3. WG Mesh and the optional traffic monitor.
4. Firecracker runtime, record validation, and the VM manager.
5. Host service and reconcilers.
6. Atlas trusted keys, then the Atlas and coordination APIs.

- The mesh and traffic monitor services are optional and can be nil.
- `connectMesh` runs on every start when `wg_mesh.enabled` is true.
- `wg_mesh.enabled = false` does not disable managed WireGuard peers.
- Both servers require a client certificate from the regional authority.
- The Atlas API also pins `tls.atlas_common_name`, because node certificates are valid clients.
- The snapshot listener uses the `metald.coordination_listen` host. That value must be one node IP address, not a wildcard.
- `wg_mesh.uplink` must name the shared VLAN interface, never its parent.

## Config keys

| Key | Default | Meaning |
|---|---|---|
| `metald.base_dir` | `/var/lib/metal` | Host state root. |
| `metald.listen` | `127.0.0.1:8080` | TCP address or `unix:/path`. |
| `metald.coordination_listen` | `127.0.0.1:9001` | Coordination address. |
| `tls.ca_file` | none | Regional authority. Required. |
| `tls.certificate_file` | none | Node certificate. Required. |
| `tls.private_key_file` | none | Node key. Required. |
| `tls.atlas_common_name` | none | Atlas client name. Required. |
| `firecracker.binary_path` | `/usr/bin/firecracker` | Firecracker binary. |
| `firecracker.sockets_dir` | `/run/metal` | API socket links. |
| `jailer.binary_path` | `/usr/bin/jailer` | Jailer binary. |
| `zfs.pool` | `metal` | ZFS pool name. |
| `wireguard.interface` | `wg0` | Underlay interface. |
| `wg_mesh.enabled` | `true` | Enables WG Mesh setup. |
| `wg_mesh.binary_path` | `/usr/local/bin/atlas-wg-mesh` | WG Mesh CLI. |
| `wg_mesh.uplink` | none | Interface for Atlas NDP. Required. |
| `wg_mesh.controller_address` | none | Atlas tenant-0 mesh address on `wg0`. VMs of tenant 0 reach it. |
| `wg_mesh.unicast` | `false` | Selects unicast NDP at start, before the first Atlas sync. |
| `traffic_monitor.enabled` | `true` | Enables idle shutdown. |
| `migration.final_delta_mib` | `512` | Delta size that triggers the final snapshot. |
| `migration.transfer_port` | `9002` | Snapshot stream port. Same on every host. |

`config.example.toml` shows the full file.

## Runtime loops

- The daemon owns all reconcilers, source streams, and snapshot uploads.
- `SIGINT` or `SIGTERM` starts a bounded graceful shutdown.
- Shutdown never stops or destroys guest VMs.

## Related

- [internal/api/SPEC.md](../../internal/api/SPEC.md) owns the server.
- [internal/firecracker/SPEC.md](../../internal/firecracker/SPEC.md) owns the runtime.
- [Integration testing](../../../docs/develop/metal-testing.md) describes host setup.
