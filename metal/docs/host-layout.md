# Metal host layout

Metal keeps virtual machine state on disk. After a restart, it reads the state
from these paths. Each virtual machine ID is a UUID V7.

```text
/etc/systemd/system/
├── metal.service                   the metald daemon, one for each host
└── metal-vm@.service               template unit; metald starts metal-vm@<uuid>

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
    └── <uuid>/                     one dir for each VM id
        ├── config.json             ID, user ID, group ID, IP, MAC, socket, and spec
        ├── jailer.env              JAILER_ARGS for metal-vm@<uuid>.service
        └── firecracker/            the exec-file name that jailer appends
            └── <uuid>/
                └── root/           the VM sees this as /
                    ├── firecracker jailer copies the exec file in
                    ├── vmlinux     hard link to the kernel
                    ├── rootfs.img  block node for the VM zvol
                    └── run/
                        └── firecracker.socket

/run/metal/                         firecracker.sockets_dir; tmpfs, remade on boot
└── <uuid>.sock                     symlink to the socket in the VM's jail

/run/netns/
└── metal-<uuid>                    the VM's network namespace
```

Inside the namespace, the virtual Ethernet pair is `vh-<uid>` on the host and `vg-<uid>` in the namespace. The guest terminal access point has a fixed name.

The jail is inside the VM directory. Removing `machines/<uuid>` removes the VM and its chroot. Jailer adds `<exec>/<uuid>/root` below the VM directory, so the jail stays separate from the VM's other files. The jail base is not configurable: the kernel is hard-linked into the jail, and a hard link cannot cross a filesystem.

A Unix socket address holds 108 bytes. The jail path repeats the VM id, so metald dials a short symlink instead of the socket path. `connect()` measures the given path, not the resolved path. The VM id therefore has no path-length limit. The veth pair uses the VM uid, so the interface-name limit does not apply to the VM id.

The ZFS pool holds the VM disks. These are dataset names, not directories:

```text
metal/images/ubuntu          read-only base zvol, with a @ready snapshot
metal/images/ubuntu@ready    source for normal VM disks
metal/vms/<uuid>             one VM disk clone
metal/staging/<image-id>     temporary read-only Machine image upload source
metal/warm/<key>@ready       local warm disk for one image and exact VM shape
```

A VM disk keeps the id of the VM that owns it, so the directory `machines/<uuid>` and the dataset `vms/<uuid>` name the same VM in two namespaces.


ZFS owns image import, local warm artifacts, image staging, and pool capacity reporting. metald does not create the pool or select its device. Host setup creates the pool before metald starts, and `zfs.pool` names it. Each imported image has an immutable manifest. Metal rejects a request that reuses an image reference with different digests or architecture.

Memory snapshots stay on the host. Atlas sends cached-image policy through `/sync`. Metal builds a memory snapshot only for an exact CPU, memory, and disk configuration. A VM cold-boots when compatible warm artifacts are not ready.
