# Image snapshots and warm artifacts

[metal SPEC](../SPEC.md) · storage detail: [internal/storage/SPEC.md](../internal/storage/SPEC.md)

Metal uses the word snapshot for two different internal flows. Public snapshots transfer a VM disk and kernel. Warm snapshots accelerate local VM boot.

## Disk vs memory snapshot

A public Machine image snapshot contains a disk and kernel. It does not contain guest memory or Firecracker state.

An internal warm snapshot contains a disk, guest memory, and Firecracker state. It stays on the host.

## Machine image staging

```text
POST /vms/{id}/snapshots
   -> generate UUIDv7
   -> pause the VM when it is running
   -> snapshot the VM disk
   -> create a read-only staging clone
   -> stage the kernel
   -> resume the VM when required
```

The response contains the snapshot ID and exact artifact sizes. Atlas uses the snapshot ID as the Machine image record name.

A staging snapshot contains no guest memory or Firecracker state. It cannot restore or roll back its source VM.

## Restore

Metal does not provide a public in-place restore operation. To use a public snapshot, Atlas creates a VM with its image reference.

Snapshot load is an internal warm start operation. Metal loads only a compatible local warm snapshot for a new VM.

## Promote to a warm image

Metal does not promote a public snapshot to a new image reference. Atlas controls public image records and object storage.

An image policy can request a local warm artifact. Metal creates it for one exact image and VM shape.

## Multipart upload

```text
POST /snapshots/{id}/upload
   -> validate each HTTP or HTTPS part URL
   -> stream rootfs from the staging ZFS volume
   -> stream the staged kernel
   -> calculate SHA-256 values
   -> return part numbers and HTTP ETag values
```

Each part is 2 GiB, except the final part. Part numbers must start at 1 and remain consecutive.

`DELETE /snapshots/{id}` removes the staging clone, source disk snapshot, and staging files. The operation is idempotent.

Metal updates activity when upload starts and after upload succeeds. The image reconciler removes staging after 48 hours without activity.

## Warm artifact creation

Warm artifacts are local cache data. Metal never uploads guest memory or Firecracker state.

Warm creation requires both `cache_image` and `memory_snapshot`. It also requires CPU, memory, and disk values.

```text
image policy
   -> create temporary warm VM
   -> cold boot without egress
   -> wait five minutes
   -> pause
   -> capture disk, Firecracker state, and memory
   -> store artifacts under the compatibility key
   -> destroy the temporary VM
```

The key includes the image reference, architecture, image digests, VM shape, and Firecracker compatibility data. Metal keeps only the required warm key for each image.

## Flow across packages

```text
api -> firecracker driver -> snapshot store -> HTTP object upload
sync -> image reconciler -> firecracker driver -> image store -> local warm cache
```

The public flow creates an image transfer. The internal flow creates a local boot optimization.

## Warm boot

A VM can use warm artifacts only when its image and shape match exactly. Metal loads the snapshot while paused, refreshes MMDS, and resumes the guest.

If warm boot fails, Metal uses cold boot. There is no public restore or promote API.

## Design notes

- Public snapshots create new immutable images instead of destructive VM rollback points.
- Metal generates the snapshot ID, so Atlas and Metal use one transfer identity.
- A read-only staging clone gives the uploader a stable root file system.
- Upload activity extends staging life for safe transfer retries.
- Warm artifacts use an exact compatibility key to prevent unsafe memory restore.
- Guest memory stays local to reduce transfer time and object storage use.
