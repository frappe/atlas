# Atlas Server module

The Server module owns provider host records, setup jobs, disk inventory, and capacity samples.

## Lifecycle

```text
Pending -> Installing -> Running <-> Stopped
              |
              v
            Failed

retained state -> Deleted
```

`Server.before_validate` ensures the named provider server. `ServerProvisioner` owns the setup sequence after document insertion.

The controller keeps the Desk methods and permission checks. The `server/core/` package owns long operations and parsing.

## Entry points

| Entry point | Purpose |
|---|---|
| `doctype/server/server.py` | Lifecycle hooks and whitelisted methods. |
| `core/provisioning.py` | Ordered and durable server setup. |
| `core/host_installation.py` | WireGuard and Metal installation. |
| `core/disk_inventory.py` | `lsblk` execution and parsing. |
| `core/catalog_sync.py` | Provider catalog persistence. |
| `usage.py` | Metal synchronization and capacity samples. |

Read the [Server lifecycle](../docs/server-lifecycle.md) for retry and failure rules.
