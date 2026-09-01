# metal HTTP API — draft

metald serves this over the unix socket `/run/metal.sock`. JSON in/out. Auth is
socket permissions only. Stateless: responses reflect host truth, so they
survive a metald restart.

**Async:** `create`/`start`/`stop`/`delete` return `202` immediately;
the client polls `GET /vms/{id}` until `state` settles. A failure shows as
`state:"failed"` + `reason`. Read-only and disk-only ops return synchronously.

## VM

```json
{
  "id": "a1b2c3d4e5f6a7b8",
  "state": "running",
  "reason": "",
  "vcpus": 2,
  "mem_mib": 512,
  "image": "ubuntu",
  "network": "default",
  "ip": "172.16.0.2",
  "mac": "02:aa:bb:cc:dd:ee",
  "pid": 41234,
  "disk": { "size_mib": 2048, "used_mib": 137, "snapshots": 2 }
}
```
`state` ∈ `created | running | paused | stopped | failed | destroyed`. `state` is
derived from the VM's systemd unit. A VM stopped on request reports `stopped`;
`failed` means the VM died on its own.

| Method | Path | Action |
|---|---|---|
| `POST` | `/vms` | create + boot → `202` |
| `GET` | `/vms` | list → `{ "vms": [VM] }` |
| `GET` | `/vms/{id}` | get (includes `disk`) |
| `POST` | `/vms/{id}/start` | boot a stopped VM → `202` |
| `POST` | `/vms/{id}/stop` | shut down → `202` |
| `POST` | `/vms/{id}/resize` | change cpu/mem/disk |
| `DELETE` | `/vms/{id}` | destroy + free → `202` |
| `GET` | `/vms/{id}/console` | stream serial console |
| `GET` | `/health` | liveness |

**Create** `POST /vms` —
`{ "vcpus", "mem_mib", "disk_mib", "image", "network", "ssh_keys" }`.
`ssh_keys` is a list of public keys, served to the guest via MMDS so it can
install them at boot. metald assigns `id`/`ip`/`mac` and boots the guest.

**Start** `POST /vms/{id}/start` — boots the guest. A stopped VM is relaunched
(a fresh jailer process) and reboots from its persisted disk and network; a
running VM → `409`.

**Stop** `POST /vms/{id}/stop` — `{ "force": false }`. `true` = SIGKILL at once.
`false` sends Ctrl+Alt+Del and gives the guest 30s to shut itself down, then
escalates to a systemd stop job. Firecracker delivers Ctrl+Alt+Del through its
emulated i8042 controller, so a guest kernel built without an i8042 keyboard
driver never sees it and always reaches the escalation. The disk, network and
state are kept, so the VM can be started again.

**Resize** `POST /vms/{id}/resize` — `{ "disk_mib" }`. `disk_mib` is grow-only; a
smaller value → `409`. The disk grows online (`zfs set volsize` + firecracker
drive rescan; the guest grows its own fs) and returns the updated VM. CPU/mem
resize is not yet implemented (`vcpus`/`mem_mib` → `501`).

## Snapshots

Disk (ZFS) snapshots of a VM — distinct from VM-state/memory snapshots (deferred).

```json
{ "name": "pre-upgrade", "vm_id": "a1b2c3d4e5f6a7b8", "size_mib": 2048, "used_mib": 12 }
```

| Method | Path | Action |
|---|---|---|
| `GET` | `/vms/{id}/snapshots` | list |
| `POST` | `/vms/{id}/snapshots` | create → `{ "name" }` |
| `DELETE` | `/vms/{id}/snapshots/{name}` | delete → `204` |
| `POST` | `/vms/{id}/snapshots/{name}/restore` | roll disk back |

**Create** is crash-consistent while running (clean/fsfreeze later, needs a guest
agent). **Restore** rolls the disk back in place (`zfs rollback -r`, so any
snapshots newer than the target are discarded); the VM keeps its id/network but
must be **stopped** → `409` otherwise.

## Errors

```json
{ "error": { "message": "image \"ubuntu\" not found" } }
```
`400` malformed · `404` unknown id/snapshot · `409` invalid state (start a
running VM, restore while running, shrink a disk) · `500` internal.

## Deferred

Image management (build/download/serve images) — to be specified.

Idempotency-key on create; `PATCH`/console interactivity; list pagination;
snapshot caps / snapshot-of-snapshot / restore-vs-pool-exhaustion.
