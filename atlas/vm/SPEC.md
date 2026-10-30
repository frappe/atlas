# Virtual machine module specification

[Atlas app specification](../SPEC.md) · Handbook: [VM lifecycle](../../docs/compute/index.md), [host selection](../../docs/compute/placement.md), [images](../../docs/storage/image-records.md), and [VM state updates](../../docs/interfaces/vm-state-updates.md).

This file identifies code owners and rules that a code change must preserve. The linked handbook pages explain the behavior.

## Code owners

| Code | Owns |
| --- | --- |
| `doctype/virtual_machine/` | VM identity, tenant permissions, and form actions. |
| `core/vm_service.py` | Changes that span an Atlas record and a Metal host. |
| `core/reconciliation.py` | Draft and termination checks after an uncertain Metal result. |
| `core/vm_sleep.py` | Scheduled idle-threshold checks, offload requests, and recovery URL refresh. |
| `core/sleepy_rebalance.py` | Automatic pressure checks, VM migration priority, and selected-only drain planning. |
| `core/placement/` | Host selection, capacity reservations, and placement strategies. |
| `core/placement/affinity.py` | Affinity rule types, their validation, and their stored JSON form. |
| `core/vm_migration.py` and `core/vm_resize.py` | Host moves and shape changes. |
| `core/metal_client.py` and `core/metal_models.py` | Metal transport, errors, and typed responses. |
| `core/image_builder.py` and `scripts/build_ubuntu*_image.sh` | Independent base and rescue recipes, shared guest setup, and purpose-specific publication. |
| `core/vm_image_transfer.py`, `core/vm_image_deletion.py`, and `core/multipart_upload.py` | Image publication and removal. |
| `core/vm_state.py` and `core/state_webhook.py` | Last host reports and messages to Central. |
| `doctype/virtual_machine_image/` | Durable image records and visibility. |
| `doctype/virtual_machine_kernel/` | One stored kernel per SHA-256 value, shared by images. |

## Rules to preserve

- `MigrationService` compares Metal Server compatibility reports for memory and offloaded migrations. Offloaded checkpoints cannot use disk-only fallback or resize. URL refresh runs before placement locks. See [migration checks](../../docs/compute/migration.md#compatibility-before-transfer).
- Migration writes lock the VM before the migration record. Cancellation survives progress polling and prevents ownership commit. After ownership commit, failures retry finish. Failed rollback retains the VM action lock until Metal confirms recovery.
- The VM record name is the Metal VM ID from the `vm-<tenant ID>-.####` series, one counter for each tenant. The number after the last dash is the VM number in the mesh address, from 1 to 65,535 within the tenant. See [address formats](../../docs/networking/address-formats.md). A deleted name is never reused.
- `VirtualMachineService.create` commits a draft and any public address request before it calls Metal. An uncertain response keeps the draft. Only confirmed Metal absence removes it.
- Metal is the authority for current host state. `Virtual Machine State` is a cache for lists and image checks. A failed Metal read must not appear as a guest state.
- `snapshot_upload_percent` is a read-only virtual field that uses the request-cached Metal response. It is absent outside an active HTTP upload. See [upload progress in Desk](../../docs/compute/sleepy-vms.md#upload-saved-snapshots).
- `VirtualMachineSleepService` reads the VM's stored offload thresholds and Metal's request time. `VirtualMachineResize` stores offload-only changes without a Metal call and merges partial changes under a VM row lock after host work. These thresholds are not part of `VirtualMachineShape`. See [automatic offload](../../docs/compute/sleepy-vms.md#configure-automatic-offload).
- The URL-refresh cache is site-scoped and keyed by VM and checkpoint. Populate it only after Metal accepts the refresh. It must not suppress offload requests or retries after a failed refresh.
- Sleepy rebalances use fresh host-matched reports and normal placement admission. Their source maintenance flag is not changed. See [automatic host rebalancing](../../docs/compute/sleepy-vms.md#automatic-host-rebalancing).
- Each sleep artifact retains its original `object_key` across host moves. `refresh_urls` signs that key and fills missing keys on the source before migration.
- Placement checks capacity under a MariaDB named lock with READ COMMITTED isolation. Keep the lock until the draft or migration reservation commits. CPU can be oversubscribed. Memory and disk cannot.
- A VM copies its image name and architecture at creation. Image references are immutable. A later VM action does not need the image record.
- The draft commits its tags and affinity rules in the same transaction as its host reservation.
- `PlacementContext` owns the affinity rules, so the strategies do not know about them. It removes the hosts that fail the rules from the snapshot, and checks the rules again under the host lock. Keep both checks: the snapshot can be 1 second old. A failed check under the lock counts as contention, so the next attempt reads a fresh snapshot.
- A migration to a destination that an operator names does not apply the affinity rules.
- A `within` rule reads the VMs of every host in the group of the candidate host. Placement locks each of those hosts without waiting, and keeps the locks until the draft commits.
- Atlas changes one network value, then sends the complete network object to Metal. Public address requests own their corresponding default routes. A per-VM file lock serializes these read-and-replace changes. A change that waits more than 10 seconds returns a conflict, and no database lock waits on Metal.
- Guest-specific keys, metadata, and mesh addresses go through Metal and guest metadata. Do not bake them into a shared image.
- Atlas SSH to a guest runs `ip netns exec metal-<id> nc 172.16.0.2 22` on the current host. It depends on the Metal namespace name and guest address in `linux_allocator.go`.
- A protected VM cannot be terminated. An unprotected Atlas record is deleted only after Metal confirms that the VM is absent.
- `ConsoleSession.close` owns console cleanup. Do not log Metal credentials.
- `ConsoleSession` reconnects after a `metald` restart as the [console guide](../../docs/compute/console.md) explains. `atlas/realtime/handlers.py` and `realtime/handlers.js` must keep the same behavior.

## Add or change behavior

- For a new host ranking rule, add a `PlacementStrategy` subclass under `core/placement/strategies/` and register it. Keep capacity checks in the shared placement path. Read [host selection](../../docs/compute/placement.md).
- For an affinity rule type, change `core/placement/affinity.py` and the matching payload models in `api/models.py` together, then regenerate the API client. Read [affinity rules](../../docs/compute/placement.md#affinity-rules).
- For create, resize, move, or delete rules, start in the owning service above. Read [VM lifecycle](../../docs/compute/index.md) and [migration](../../docs/compute/migration.md).
- For image transfer or deletion, start in the image owner above. Read [image records](../../docs/storage/image-records.md).
- For guest network values and public addresses, read [VM records](../../docs/compute/vm-records.md), [host networking](../../docs/networking/host-networking.md), and [public IPs](../../docs/networking/public-ips.md).
- For Central notifications, read [VM state updates](../../docs/interfaces/vm-state-updates.md) and the [tenant API](../../docs/interfaces/tenant-api.md).

## Rescue

`VirtualMachineService.set_rescue` selects the newest enabled, Available image tagged `purpose=rescue` for the VM architecture for a new session and resolves the pinned artifact identity for URL renewal. `MetalRescue` carries host state. The DocType and tenant API enforce permissions. See [rescue mode](../../docs/compute/rescue.md) for image selection and recovery behavior.

Responses from older Metal hosts can omit rescue fields. The parser defaults missing fields to inactive rescue and rejects malformed values.

`scripts/guest/rescue.py` owns guided operations and offline inventory. The menu invokes that CLI. Managed operations share a systemd unit and file lock; the expert shell bypasses that lock. `core/test_rescue_tools.py` covers disk access, password input, discovery, and job completion.

## Validation

Tests sit beside the code in `core/test_*.py`, `core/placement/test_*.py`, and `doctype/*/test_*.py`. The [Metal VM specification](../../metal/internal/vm/SPEC.md) covers the other side of the request.
