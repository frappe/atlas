# Metal host layout

Metal keeps virtual machine state on disk. After a restart, it reads the state from these paths. The controller supplies each virtual machine ID.

```text
/etc/systemd/system/
├── metal.service                   the metald daemon, one for each host
└── metal-vm@.service               template unit; metald starts metal-vm@<id>

/var/lib/metal/
├── metald.toml                     metald configuration; metald.base_dir is this dir
├── images/                         derived from metald.base_dir
│   └── ubuntu/                     one directory for each image reference
│       ├── manifest.json           immutable digests and architecture
│       ├── vmlinux                 guest kernel, hard-linked into the jail
│       ├── boot-args               kernel command line, optional
│       ├── last-used               last successful VM start
│       └── warm/<key>/              local warm artifacts for one exact shape
│           ├── state               Firecracker device state
│           └── memory              guest memory
├── snapshots/<id>/                 temporary Machine image staging
│   ├── metadata.json
│   └── vmlinux
├── image-policies.json             desired cached images from the controller
├── wireguard-peers.json            atomically saved managed peer set
└── machines/                       derived from metald.base_dir
    └── <id>/                       one directory for each VM ID
        ├── config.json             reservation, desired state, and cleanup progress
        ├── status.json             observed state and reconciliation error
        ├── jailer.env              JAILER_ARGS for metal-vm@<id>.service
        └── firecracker/            the executable name that jailer appends
            └── <id>/
                └── root/           the VM sees this as /
                    ├── firecracker jailer copies the exec file in
                    ├── vmlinux     hard link to the kernel
                    ├── rootfs.img  block node for the VM zvol
                    └── run/
                        └── firecracker.socket

/run/metal/                         firecracker.sockets_dir; tmpfs, remade on boot
└── <id>.sock                       link to the socket in the VM jail

/run/netns/
└── metal-<id>                      the VM network namespace
```

The host veth is `vh-<user-id>`. The namespace veth is `vg-<user-id>`. The TAP name is `tap0`.

The jail is inside the VM directory. Removing `machines/<id>` removes the VM and its chroot. Jailer adds `<exec>/<id>/root` below the VM directory, so the jail stays separate from the VM's other files. The jail base is not configurable: the kernel is hard-linked into the jail, and a hard link cannot cross a filesystem.

A Unix socket address holds 108 bytes. Metal uses a short link because the jail socket path can exceed this limit. The veth names use the VM user ID to meet the Linux interface name limit.

The ZFS pool holds the VM disks. These are dataset names, not directories:

```text
metal/images/ubuntu          base ZFS volume with a @ready snapshot
metal/images/ubuntu@ready    source for normal VM disks
metal/vms/<id>               one VM disk clone
metal/staging/<image-id>     temporary read-only Machine image upload source
metal/warm/<key>@ready       local warm disk for one image and exact VM shape
```

A VM disk keeps the VM ID. The paths `machines/<id>` and `vms/<id>` identify the same VM.

The storage stores own image import, local warm artifacts, image staging, VM disks, and pool capacity. metald does not create the pool or select its device. Host setup creates the pool before metald starts, and `zfs.pool` names it. Each imported image has an immutable manifest. Metal rejects a request that reuses an image reference with different digests or architecture.

Memory snapshots stay on the host. Atlas sends cached-image policy through `/sync`. Metal builds a memory snapshot only for an exact CPU, memory, and disk configuration. A VM cold-boots when compatible warm artifacts are not ready.
