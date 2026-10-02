# host: controller synchronization and capacity

For Go code, follow the repository [Go anti-pattern rules](../../../llm/go-code-review-guide.md).

[Metal specification](../../SPEC.md) · Behavior: [host sync](../../../docs/region/host-sync.md)

## Purpose

The `host` package applies the controller-owned sets: WireGuard peers, image policies, privileged VM addresses, and WireGuard gateway return routes. Each complete set replaces the previous one.

## Types

| Type | Responsibility |
|---|---|
| `Service` | Applies desired host state, then reports the sync result. |
| `DesiredState` | The 3 controller-owned sets, each complete. |
| `SyncResult` | What one sync returns: capacity and VM states. |
| `Capacity` | Host capacity for placement. |
| `PrivilegedMesh`, `WireGuardManager`, `ImagePolicyStore`, `VirtualMachineSource`, `StorageCapacitySource` | Injected services. |

- `Service` holds no state. Each injected service owns the state it applies.
- The mesh service is nil when Atlas WG Mesh is disabled.

## Synchronize

`POST /v1/sync` runs these steps in order and stops at the first error:

1. Apply the privileged VM addresses.
2. Apply the WireGuard peers.
3. Set the image policies.
4. Set the WireGuard gateway return routes. `vm.Manager` stores them and adds them to each VM with `is_accessible_via_wireguard_gateway`. A VM route with the same destination wins.
5. Wake the reconcilers.
6. List the VM state once.
7. Return the `SyncResult`.

`Wake` must follow the writes, so reconcilers act on the new policies at once.

## Capacity

| Resource | Source |
|---|---|
| CPU total | Host CPU count × 1000 millicores. |
| CPU available | Total minus VM `cpu_millicores`, floored at zero. |
| Memory | `MemAvailable` from `/proc/meminfo`. |
| Storage | `StorageCapacitySource`. |

- CPU is for visibility only. No admission check uses it.
- `VirtualMachineSource` skips a VM that has only one of its two records.

## Virtual machine states

`SyncResult.VirtualMachineStates` maps each VM ID to its last observed state. It reuses the capacity `List` call, which reads stored records, not the runtime.

## Related

- [internal/api/SPEC.md](../api/SPEC.md) owns the sync request and response shapes.
- [internal/network/SPEC.md](../network/SPEC.md) owns WireGuard peers and the privileged mesh.
- [internal/storage/SPEC.md](../storage/SPEC.md) owns image policies and pool capacity.
