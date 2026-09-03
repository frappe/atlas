# storage: ZFS-backed VM disks

[internal SPEC](../SPEC.md) · overview: [docs/storage.md](../../docs/storage.md)

## Purpose

Package `storage` turns an image ref into a disk ready for a virtual machine. The
premise is one choice: a VM disk is a copy-on-write clone of a base ZFS image. That
choice makes a create near-instant, keeps many VMs from one image cheap, and yields
snapshots and warm images from the same primitives. The kernel is a hard-linked
file. The disk is a block device inside the jailer chroot.

## Types

The `ZFS` struct holds only `pool`, `kernelDir`, and `imagesDir`. It keeps no other
state. The real state lives in ZFS datasets and files on disk.

| Type | Role |
|---|---|
| `Resolver` | Interface: `Prepare`, `PrepareRootfs`, `Release`, `Resize`, `Snapshot`, `Snapshots`, `DeleteSnapshot`, `Restore`, `Usage`, `Promote`, `Images`, `DeleteImage`, `ImageMemory`. |
| `ZFS` | The one implementation. `NewZFS(pool, kernelDir, imagesDir)`. |
| `Request` | `Prepare` input: `VMID`, `Ref`, `ChrootRoot`, `UID`/`GID`, `DiskMiB` (0 keeps the base size). |
| `BootConfig`, `Drive` | What firecracker needs to boot: kernel path, args, drives. |
| `SnapshotInfo`, `Usage`, `ImageInfo` | Read models: sizes, snapshot count, `Warm` flag. |
| `PromoteRequest` | Build image `Ref` from a VM snapshot. |
| `ErrNotFound`, `ErrInUse` | Sentinels. `ErrInUse` = the image still has VM clones. |

## Dataset layout

A `zvol` is a ZFS block-device volume. `@ready` is the named snapshot that VM disks
clone from. The VM id and its disk share the same id.

```text
<pool>/images/<ref>          base image zvol, treated as read-only
<pool>/images/<ref>@ready    snapshot each VM clones from
<pool>/vms/<id>              VM rootfs, a clone of @ready       id = VM UUID
<pool>/vms/<id>@<name>       one disk snapshot of that VM
/dev/zvol/<pool>/vms/<id>    block-device node, made by udev

kernelDir/<ref>/vmlinux      kernel, hard-linked into the chroot
kernelDir/<ref>/boot-args    optional kernel cmdline, else a default
imagesDir/<ref>/{state,mem}  warm-image memory capture
```

## Clone and snapshot lineage

`zfs clone` shares the base's blocks until written, so a clone is cheap and grows
only as the VM writes. `zfs send | zfs recv` streams a full independent copy.

```text
images/<ref> --@ready--> (zfs clone) --> vms/<id> --@snapA, @snapB   COW snapshots

Promote:
  vms/<id>@snapA --[zfs send | zfs recv]--> images/<newref>    full copy, no shared blocks
  then: destroy the received @snapA, take images/<newref>@ready,
        copy the kernel and the state+mem files into the image store
```

Promote makes a standalone warm image, so the source VM can be deleted afterward.

## Provision flow

```text
Prepare(req):
  mkdir ChrootRoot
  hard-link kernelDir/<ref>/vmlinux -> ChrootRoot/vmlinux
  provisionDisk:
     if vms/<id> absent:  zfs clone images/<ref>@ready vms/<id>   (created = true)
     grow: zfs set volsize=<DiskMiB>M   only when larger; the guest grows its own fs
     mknod block node /dev/zvol/.../vms/<id> -> ChrootRoot/rootfs.img, chown uid:gid
     on error after a fresh clone -> rollback = Release (zfs destroy -r)
  return BootConfig{Kernel: /vmlinux, KernelArgs, Drives: [/rootfs.img root rw]}
```

The disk survives a stop. A restart reuses the clone, so `provisionDisk` clones only
when the disk is absent. Rollback destroys the disk only when this call created it,
so a restart never discards data. `PrepareRootfs` is the same without the kernel
link or boot config, for warm-start or restore, where the kernel lives inside the
memory snapshot.

## Snapshot and image operations

| Operation | ZFS command | Note |
|---|---|---|
| `Snapshot` | `zfs snapshot <vm>@<name>` | Cheap. Pins the current blocks. |
| `Restore` | `zfs rollback -r <vm>@<name>` | Reverts the disk. `-r` discards newer snapshots. The VM must be stopped. |
| `DeleteSnapshot` | `zfs destroy <vm>@<name>` | One snapshot. |
| `Release` | `zfs destroy -r <vm>` | The disk and every snapshot under it. Idempotent. |
| `DeleteImage` | `zfs destroy -r <base>` | `ErrInUse` while a VM clone still depends on `@ready`. |
| `Resize` | `zfs set volsize=<N>M` | Grow-only. |

Full concept: [docs/snapshots.md](../../docs/snapshots.md).

## Chroot materialization

```text
kernel:      hard-link kernelDir/<ref>/vmlinux -> ChrootRoot/vmlinux
rootfs:      mknod block node ChrootRoot/rootfs.img mirroring /dev/zvol/.../vms/<id>,
             chown uid:gid so the jailed firecracker can open it
             udev makes the /dev/zvol symlink async; statBlock waits up to ~3 s
warm mem:    LinkOrReflink stages a large snapshot memory file:
             hard link on one filesystem, else cp --reflink=auto (COW), else full copy
```

`reflink` is a copy-on-write file copy: the two files share blocks until one is
written. The image memory store and the chroots sit on one filesystem, so staging a
warm memory file is a hard link with no data copy.

## Related

- [docs/storage.md](../../docs/storage.md) broad storage overview.
- [docs/snapshots.md](../../docs/snapshots.md) disk and memory snapshots, warm images.
- [internal/firecracker/SPEC.md](../firecracker/SPEC.md) the caller: boot, warm-start, restore.
- [docs/host-layout.md](../../docs/host-layout.md) the on-disk paths and dataset names.
