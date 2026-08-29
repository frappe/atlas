# metal HTTP API — draft

metald serves this over the unix socket `/run/metal.sock`. JSON in/out. Auth is
socket permissions only. Stateless: responses reflect host truth, so they
survive a metald restart.

**Async:** `create`/`start`/`stop`/`resize`/`delete` return `202` immediately;
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
`state` ∈ `created | running | paused | stopped | failed | destroyed`.

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

**Create** `POST /vms` — `{ "vcpus", "mem_mib", "disk_mib", "image", "network" }`.
metald assigns `id`/`ip`/`mac` and boots the guest.

**Stop** `POST /vms/{id}/stop` — `{ "force": false }`. `false` = Ctrl+Alt+Del,
`true` = SIGKILL.

**Resize** `POST /vms/{id}/resize` — `{ "vcpus"?, "mem_mib"?, "disk_mib"? }`. All
optional; omitted = unchanged; `disk_mib` is grow-only. A disk-only change grows
online (`lvextend` + firecracker rescan; guest grows its own fs). Firecracker
can't hotplug cpu/mem, so if `vcpus` and/or `mem_mib` change metald **restarts
the VM** to apply them (brief downtime).

## Snapshots

Disk (LVM) snapshots of a VM — distinct from VM-state/memory snapshots (deferred).

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
agent). **Restore** replaces the disk in place; VM keeps its id/network but must
be **stopped** → `409` otherwise.

## Errors

```json
{ "error": { "message": "image \"ubuntu\" not found" } }
```
`400` malformed · `404` unknown id/snapshot · `409` invalid state (start a
running VM, restore while running, shrink a disk) · `500` internal.

## Deferred

Idempotency-key on create; `PATCH`/console interactivity; list pagination;
snapshot caps / snapshot-of-snapshot / restore-vs-pool-exhaustion.
