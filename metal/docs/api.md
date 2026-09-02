# metal HTTP API — draft

metald generates this application programming interface specification from the
handler annotations, embeds it, and serves it at `/docs`. `make build` regenerates it. The file is
`internal/api/swagger.json`.

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
| `POST` | `/vms` | create + boot (warm-start from a warm image) → `202` |
| `GET` | `/vms` | list → `{ "vms": [VM] }` |
| `GET` | `/vms/{id}` | get (includes `disk`) |
| `POST` | `/vms/{id}/start` | boot a stopped VM → `202` |
| `POST` | `/vms/{id}/stop` | shut down → `202` |
| `POST` | `/vms/{id}/pause` | pause the guest (halt vCPUs) |
| `POST` | `/vms/{id}/resume` | resume a paused guest |
| `POST` | `/vms/{id}/resize` | change cpu/mem/disk |
| `DELETE` | `/vms/{id}` | destroy + free → `202` |
| `GET` | `/vms/{id}/console` | stream serial console |
| `GET` | `/health` | liveness |

**Create** `POST /vms` —
`{ "vcpus", "mem_mib", "disk_mib", "image", "network", "ssh_keys" }`.
`ssh_keys` is a list of public keys, served to the guest via MMDS so it can
install them at boot. metald assigns `id`/`ip`/`mac` and boots the guest. If
`image` names a warm image (see Images), the VM warm-starts from the image's
captured memory instead of a cold boot.

**Pause / Resume** `POST /vms/{id}/pause` halts the guest's vCPUs and reports
`state:"paused"`; `POST /vms/{id}/resume` returns it to `running`. Pause holds the
process and its memory, unlike stop, which frees them.

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

A snapshot captures a VM's disk, and optionally its memory. Set `memory:true` to
also capture guest RAM and device state, paired with the disk snapshot so a restore
is consistent.

```json
{ "name": "pre-upgrade", "vm_id": "a1b2c3d4e5f6a7b8", "memory": true,
  "size_mib": 2048, "used_mib": 12, "created_at": "2026-09-02T10:00:00Z" }
```

| Method | Path | Action |
|---|---|---|
| `GET` | `/vms/{id}/snapshots` | list → `{ "snapshots": [Snapshot] }` |
| `POST` | `/vms/{id}/snapshots` | create → `{ "name", "memory" }` |
| `DELETE` | `/vms/{id}/snapshots/{name}` | delete → `204` |
| `POST` | `/vms/{id}/snapshots/{name}/restore` | restore in place |
| `POST` | `/vms/{id}/snapshots/{name}/promote` | promote to an image → `202` |

**Create** `POST /vms/{id}/snapshots` — `{ "name", "memory": false }`.
`memory:false` is a disk-only ZFS snapshot, taken with no pause. `memory:true`
pauses the guest, takes the disk snapshot, writes the memory and device state, then
resumes; the pause is brief and happens once. The guest must be booted.

**Restore** `POST /vms/{id}/snapshots/{name}/restore` rolls the disk back
(`zfs rollback -r`, so snapshots newer than the target are discarded). When the
snapshot has memory, it also reloads RAM so the VM resumes at the captured instant.
metald stops the VM and brings it back; a memory-less snapshot cold-boots from the
rolled-back disk on the next start.

**Promote** `POST /vms/{id}/snapshots/{name}/promote` — `{ "image": "<ref>" }`.
Builds a standalone warm image from the snapshot: a full independent disk copy
(`zfs send | zfs receive`), the kernel, and the memory files. The image shares no
lineage with the VM, so the VM can be deleted afterward. Requires a `memory:true`
snapshot.

## Images

An image is a template that VMs are created from. A **warm** image also carries a
memory capture, so a VM created from it starts from restored RAM instead of a cold
boot. Promote is how a warm image is made.

```json
{ "ref": "golden", "warm": true, "size_mib": 2048, "created_at": "2026-09-02T10:05:00Z" }
```

| Method | Path | Action |
|---|---|---|
| `GET` | `/images` | list → `{ "images": [Image] }` |
| `DELETE` | `/images/{ref}` | delete → `204` |

**Create a VM from an image** is the normal `POST /vms` with `image` set to the
ref. A warm image is detected automatically: the VM loads the captured RAM,
refreshes its SSH keys and clock through MMDS, and resumes. A cold image boots as
usual.

**Delete** `DELETE /images/{ref}` frees the image. It returns `409` while any VM
created from it still exists, so destroy those VMs first.

## Errors

```json
{ "error": { "message": "image \"ubuntu\" not found" } }
```
`400` malformed · `404` unknown id/snapshot/image · `409` invalid state (start a
running VM, shrink a disk, snapshot a VM that has not booted, promote a
memory-less snapshot, delete an image that still has clones) · `500` internal.

## Deferred

Image build/download from external sources (promote is the only way to make an
image today).

Idempotency-key on create; `PATCH`/console interactivity; list pagination; diff
(incremental) memory snapshots; snapshot caps / snapshot-of-snapshot /
restore-vs-pool-exhaustion.
