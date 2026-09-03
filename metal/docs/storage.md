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
