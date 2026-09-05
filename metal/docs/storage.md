# Storage

[metal SPEC](../SPEC.md) · detail: [internal/storage/SPEC.md](../internal/storage/SPEC.md)

Metal uses ZFS for image volumes and virtual machine disks. A VM disk is a copy-on-write clone of an image snapshot.

## Storage owners

| Type | Responsibility |
|---|---|
| `ZFSPool` | Pool capacity and ZFS names. |
| `VirtualMachineStore` | VM disk preparation, resize, usage, and release. |
| `ImageStore` | Image import, policy, pruning, and warm artifacts. |
| `SnapshotStore` | Image staging, upload, deletion, and cleanup. |

`NewStores` creates these services with one shared `ZFSPool`.

## Image and clone model

```text
<pool>/images/<image>@ready
              |
              └─ zfs clone -> <pool>/vms/<vm-id>
```

Image import downloads and verifies the root file system and kernel. Metal stores an immutable manifest for the image reference.

## Disk lifecycle

VM disk preparation clones the image only when the VM disk is absent. Restarting a cold VM reuses its disk.

Disk resize is grow-only. A running VM receives a Firecracker drive update after the ZFS volume grows.

Terminate removes the VM dataset and its snapshots after process and network cleanup.

## Image cache

`POST /sync` replaces the complete image policy set. Cached images for the host architecture download in the background.

A successful VM start updates `last-used`. Metal prunes an image after 24 idle hours when policy does not retain it. A dependent VM disk prevents image deletion.

## Chroot substrate

Metal hard-links the kernel into the jail. It creates a block device node for the VM ZFS volume.

`LinkOrCopy` uses a hard link when possible. Otherwise, it uses `cp --reflink=auto`.

## Snapshots and images

### Snapshot staging

A public snapshot creates an immutable image transfer. It does not create a VM rollback point.

The snapshot store creates a source disk snapshot, a read-only staging clone, a kernel file, and metadata. See [docs/snapshots.md](snapshots.md).

### Warm artifacts

Memory and Firecracker state stay on the host. They are stored by image reference and an exact compatibility key.

Warm disk artifacts use `<pool>/warm/<key>@ready`. They are separate from imported image volumes.

## Design notes

- ZFS clone creation keeps VM disk reservation fast.
- Immutable manifests prevent one image reference from changing content.
- Grow-only resize avoids host-side filesystem shrink risk.
- Separate stores keep pool, VM disk, image, and staging state with one owner.
- Image policy controls retention, not whether a VM can download an image.
- Memory and Firecracker state stay on the host.

## Throughput and IOPS limits

The VM configuration can set `disk.throughput_mbps` and `disk.iops`. Each limit covers reads and writes together. A value of `0` does not apply a limit.

Metal sets a Firecracker drive rate limiter. Each limit becomes one token bucket that refills every second. `PATCH /drives/{id}` changes the buckets on a running VM, so a limit change needs no restart.

Metal does not use the cgroup IO controller. ZFS schedules its own IO through the ARC and the transaction group pipeline, so a block-level limit does not hold for a dataset.
