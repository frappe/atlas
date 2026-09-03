# Storage

[metal SPEC](../SPEC.md) · overview: [docs/architecture.md](architecture.md)

metal stores each VM's disk on ZFS. The premise is one choice: a VM disk is a
copy-on-write clone of a base image. That choice makes a create near-instant, keeps
many VMs from one image cheap, and gives snapshots and warm images from the same
primitives. Detail: [internal/storage/SPEC.md](../internal/storage/SPEC.md).

## Image and clone model

```text
images/<ref>        base image (a zvol), treated as read-only
   |  @ready snapshot
   v
vms/<id>            each VM's disk, a clone of images/<ref>@ready
```

A clone shares the base's blocks until the VM writes, so it starts near zero size
and grows only as it diverges. Detail:
[internal/storage/SPEC.md](../internal/storage/SPEC.md).

## Disk lifecycle

```text
create   zfs clone images/<ref>@ready vms/<id>
grow     zfs set volsize   grow-only; the guest resizes its own filesystem
persist  the disk survives a stop; a restart reuses it
free     zfs destroy -r vms/<id>   on Destroy
```

## Snapshots and images

A disk snapshot is a cheap ZFS snapshot. A memory snapshot pairs it with captured
RAM. Promote copies a snapshot into a standalone warm image with `zfs send | zfs
recv`, so the image shares no blocks with the VM and outlives it. Full concept:
[docs/snapshots.md](snapshots.md). Detail:
[internal/storage/SPEC.md](../internal/storage/SPEC.md).

## Chroot substrate

The VM disk is a real block device (a zvol). metald exposes it inside the jailer
chroot as a block node, and hard-links the kernel next to it. Detail:
[internal/storage/SPEC.md](../internal/storage/SPEC.md) and
[docs/host-layout.md](host-layout.md).

## Design notes

Why the main choices were made.

- Clone, not copy. `zfs clone` from `@ready` is near-instant and shares the base's
  blocks until written, so VMs cost almost no space until they diverge. A clone needs
  a snapshot source, hence the immutable `@ready`.
- Grow-only disks. Shrinking under a live filesystem can drop data. Growing is safe:
  the guest extends its own filesystem (`growpart`, `resize2fs`). A smaller resize is
  rejected upstream.
- Disk persists across a stop. `provisionDisk` clones only when the disk is absent, so
  a restart reuses it. Rollback destroys the disk only if this call created it, so a
  restart never discards data.
- Snapshot is cheap; restore is destructive. `zfs snapshot` just pins blocks. `zfs
  rollback -r` reverts and discards newer snapshots, since ZFS rolls back only to the
  latest. The VM must be stopped, or the revert corrupts a live guest.
- Promote copies in full. A promoted image must outlive its VM. A clone would block
  the VM's deletion or share its fate. `zfs send | zfs recv` makes a standalone
  dataset, so the VM can be deleted after. The cost is a full copy.
- Block node, not a file. The disk is a zvol. The jail cannot reach `/dev`, so metald
  makes a block node in the chroot mirroring `/dev/zvol/...`, owned by the VM uid.
  udev creates the symlink async, so `statBlock` waits up to ~3 s.
- Hard-link the kernel. A hard link is free and shares the read-only kernel. It cannot
  cross a filesystem, so `kernelDir` sits with the chroots.
- Memory files are 0644. On warm start any VM uid hard-links an image's `state` and
  `mem` files into its chroot, so they are world-readable on a trusted host.

Detail: [internal/storage/SPEC.md](../internal/storage/SPEC.md).
