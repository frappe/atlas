# Host layout

This page shows every file and dataset that Metal keeps on a host. After a restart, Metal rebuilds its view from these paths, systemd, and ZFS. It keeps nothing else. The VM ID in each path is the ID that the Atlas app chose.

## Files

```text
/etc/systemd/system/
├── metal.service                  the metald daemon
└── metal-vm@.service              one unit per VM: metal-vm@<id>

/var/lib/metal/                    metald.base_dir
├── metald.toml                    metald configuration
├── image-policies.json            cached images requested by host sync
├── wireguard-peers.json           managed WireGuard peer set
├── images/<image-ref>/
│   ├── manifest.json              digests and architecture. Never changes
│   ├── vmlinux                    guest kernel, hard-linked into each jail
│   ├── boot-args                  kernel command line (optional)
│   ├── last-used                  time of the last VM start
│   └── warm/<key>/                warm boot artifact for one exact VM shape
│       ├── state                  Firecracker device state
│       └── memory                 guest memory
├── snapshots/<snapshot-id>/       Machine image staging
│   ├── metadata.json
│   └── vmlinux
└── machines/<vm-id>/
    ├── config.json                desired state and reservation
    ├── status.json                observed state and cleanup progress
    ├── metrics/                  seven days of daily VM usage files
    ├── jailer.env                 arguments for metal-vm@<id>
    ├── migration/                 only while a migration holds this VM
    │   ├── destination.json       destination reservation and progress
    │   └── source.json            source lock
    ├── saved-state/               idle shutdown: state, memory, metadata
    └── firecracker/<vm-id>/root/  the jail. The VM sees this as /
        ├── firecracker            copied in by jailer
        ├── vmlinux                hard link to the kernel
        ├── rootfs.img             block device node for the VM disk
        └── run/firecracker.socket

/run/metal/<vm-id>.sock            short link to the Firecracker API socket
/run/netns/metal-<vm-id>           the VM network namespace
```

The host veth is `vh-<user-id>`, the namespace veth is `vg-<user-id>`, and the TAP is `tap0`. Names come from the VM's host user ID, so Metal stores no name allocation.

## ZFS datasets

These are dataset names in the configured pool, not directories:

```text
<pool>/images/<image-ref>          base volume
<pool>/images/<image-ref>@ready    source snapshot for VM disks
<pool>/vms/<vm-id>                 one VM disk clone
<pool>/staging/<snapshot-id>       read-only Machine image upload source
<pool>/warm/<key>@ready            warm boot disk for one image and VM shape
<pool>/state                       file system mounted at /var/lib/metal
```

`machines/<id>` and `vms/<id>` name the same VM. Host setup creates the pool before `metald` starts. `metald` never selects a device.

## Why it is laid out this way

- **The jail lives inside the VM directory.** Removing `machines/<id>` removes the VM and its chroot. The kernel is hard-linked into the jail, and a hard link cannot cross filesystems, so the jail base is fixed.
- **Saved state is published atomically.** Metal writes `saved-state-pending/` inside the jail, then renames it into place.
- **Sockets use short links.** A Unix socket path holds 108 bytes, and a jail path can be longer, so `/run/metal` holds short links. It is a tmpfs, rebuilt at boot.

::: details Source code and tests

- [Storage specification](../../metal/internal/storage/SPEC.md) owns datasets and image files.
- [Firecracker specification](../../metal/internal/firecracker/SPEC.md) owns the jail and socket paths.
- [VM specification](../../metal/internal/vm/SPEC.md) owns the VM record files.

:::
