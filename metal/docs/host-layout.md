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
│   └── ubuntu/                     one dir for each warm image ref
│       ├── state                   firecracker device state
│       └── mem                     guest memory, hard-linked into a jail
├── kernels/                        derived from metald.base_dir
│   └── ubuntu/                     one dir for each image ref
│       ├── vmlinux                 guest kernel, hard-linked into the jail
│       └── boot-args               kernel cmdline, optional
└── machines/                       derived from metald.base_dir
    └── <uuid>/                     one dir for each VM id
        ├── config.json             ID, user ID, group ID, IP, MAC, socket, and spec
        ├── jailer.env              JAILER_ARGS for metal-vm@<uuid>.service
        ├── warmload                marker: the next start loads a warm image
        ├── snapshots/              one dir for each memory snapshot
        │   └── <name>/
        │       ├── state
        │       └── mem
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
metal/images/ubuntu       read-only base zvol, with a @ready snapshot
metal/images/ubuntu@ready the source that each VM disk clones
metal/vms/<uuid>          the VM disk, a clone of the base snapshot
```

A VM disk keeps the id of the VM that owns it, so the directory `machines/<uuid>` and the dataset `vms/<uuid>` name the same VM in two namespaces.

The images directory shares a filesystem with the jails, because a warm start
hard-links an image's memory file into the VM's chroot. Every directory metald
uses comes from `metald.base_dir`, so that holds by construction.

ZFS is the only storage backend. metald does not create the pool or select its device. Host setup creates the pool before metald starts, and `zfs.pool` names it.
