# platform: host integration

For Go code, follow the repository [Go anti-pattern rules](../../../llm/go-code-review-guide.md).

[Metal specification](../../SPEC.md) · overview: [Metal overview](../../../docs/develop/metal.md)

## Purpose

Package `platform` is the Metal boundary to host files, commands, and systemd. Metal talks to systemd through D-Bus, not `systemctl`.

## Types

| Type | Owns |
|---|---|
| `UnitManager` | The systemd operations a VM runtime needs. |
| `DBus` | The `UnitManager` implementation and the system-bus connection. |
| `Status`, `Result`, `Limits` | systemd state across the boundary. |
| `FileDescriptorStore` | Open descriptors held by systemd across a restart. |

## Host commands

| Function | Returns |
|---|---|
| `Run` | Success or failure. |
| `Output` | stdout. |
| `RunInNetworkNamespace` | stdout of `ip netns exec`. |

Each error keeps the command diagnostics. A context can stop a running command.

## Systemd units

- The `metal-vm@.service` template runs each VM.
- Start and stop wait for systemd to finish. A replace job makes the newest request win.
- Reset failed ignores an absent unit.
- The context cancels systemd waits and polling.
- The `firecracker` package maps unit state to VM state.
- `GetUsage` reads CPU, memory, and root disk I/O counters from the VM cgroup. The VM unit must have `IOAccounting=yes`; the reader selects the root disk device in `io.stat`.

## File descriptor store

- `FileDescriptorStore` stores with `FDSTORE=1` and removes with `FDSTOREREMOVE=1`.
- The unit needs `NotifyAccess`, `FileDescriptorStoreMax`, and `FileDescriptorStorePreserve=yes`.
- Without the notification socket, `IsAvailable` is false and store operations do nothing.
- Without `FileDescriptorStorePreserve=yes`, a service stop releases the descriptors.

## Tests

Unit tests sit next to the code, such as `systemd_dbus_test.go` and `systemd_fdstore_test.go`.

## Related

- [Metal reconciliation](../../../docs/compute/reconciliation.md) describes the VM state machine.
- [internal/firecracker/SPEC.md](../firecracker/SPEC.md) maps unit state to VM state.
