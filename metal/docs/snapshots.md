# Snapshots and warm images

[metal SPEC](../SPEC.md) · overview: [docs/architecture.md](architecture.md)

A snapshot captures a VM's disk, and optionally its memory. From a memory snapshot,
metal can restore the exact running instant, or promote it into a warm image that
new VMs boot from. This spans three packages, so it has its own overview. Detail:
[internal/storage/SPEC.md](../internal/storage/SPEC.md) and
[internal/firecracker/SPEC.md](../internal/firecracker/SPEC.md).

## Disk vs memory snapshot

```text
disk snapshot     zfs snapshot of the disk. Cheap. No pause.
memory snapshot   pause -> disk snapshot + capture RAM and device state -> resume
```

The memory capture is paired with the disk snapshot, so a restore is consistent.
Detail: [internal/firecracker/SPEC.md](../internal/firecracker/SPEC.md).

## Restore

```text
memory snapshot   reload RAM, resume at the captured instant
disk-only         roll the disk back, cold-boot on the next start
```

Restore stops the VM first, rolls the disk back, and discards newer snapshots.
Detail: [internal/storage/SPEC.md](../internal/storage/SPEC.md).

## Promote to a warm image

```text
vms/<id>@snap --[zfs send | zfs recv]--> images/<newref>   full copy, no shared blocks
             + copy the kernel and the state and mem files
```

A promoted image is standalone, so the source VM can be deleted. A VM created from a
warm image loads the captured RAM instead of cold-booting. Detail:
[internal/storage/SPEC.md](../internal/storage/SPEC.md) and
[docs/vm.md](vm.md).

## Flow across packages

```text
api  ->  firecracker (pause/capture/resume, move files, load)  ->  storage (ZFS)
```

firecracker drives the pause, capture, and file staging. storage owns the ZFS disk
snapshot, rollback, and the send/recv copy. Detail:
[internal/firecracker/SPEC.md](../internal/firecracker/SPEC.md).
