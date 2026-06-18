# Filesystem layout on the server

Everything Atlas puts on a server lives under `/var/lib/atlas/`. Nothing
else.

```
/var/lib/atlas/
├── images/
│   └── ubuntu-24.04/
│       ├── vmlinux-noble-server      # kernel binary, immutable per image
│       └── ubuntu-24.04-server.ext4  # pristine rootfs, immutable per image
│
├── virtual-machines/
│   ├── d4f7c1a2-7e0a-4f1b-93cc-ad96b9b39b3e/
│   │   ├── jail/                     # jailer chroot base for this VM
│   │   │   └── firecracker/<uuid>/root/   # the jail root (per-VM uid owns it)
│   │   │       ├── firecracker        # copied in by the jailer
│   │   │       ├── firecracker.json   # config, jail-relative paths
│   │   │       ├── rootfs.ext4        # block-special node → the VM's disk LV (mknod'd in, per-VM uid)
│   │   │       ├── data.ext4          # block-special node → the VM's data disk LV (only if it has one)
│   │   │       ├── vmlinux            # hard-link to the image kernel
│   │   │       ├── run/firecracker.socket  # Firecracker API socket
│   │   │       ├── metadata.json      # warm clones only: the MMDS identity payload (staged by provision)
│   │   │       └── snapshot/          # pending memory snapshot (fast stop/start OR a staged warm pair), absent after a cold stop
│   │   │           ├── vmstate.bin    # Firecracker vmstate (warm clone: hard link to the golden's)
│   │   │           ├── mem.bin        # guest RAM (warm clone: hard link, CoW-shared by N clones)
│   │   │           ├── host-signature.json  # warm clones only: vm-restore.py's compatibility guard
│   │   │           └── READY          # marker: the pair is complete; consumed by vm-restore.py
│   │   ├── network.env               # TAP/IPV6 + IPV4_HOST/GUEST_CIDR + netns + veth names
│   │   ├── jailer-launch.sh          # generated launcher the unit execs (uid/gid, netns, cgroup/rlimit baked in)
│   │   └── log/
│   │       └── firecracker.log
│   ├── 19ae...                       # one directory per VM, named by UUID
│   └── ...
│
├── snapshots/                        # durable warm-golden artifacts, one dir per Warm snapshot row
│   └── <snapshot-uuid>/
│       ├── vmstate.bin               # captured at one paused instant with the disk LV
│       ├── mem.bin                   # the golden's RAM; hard-linked (read-only) into clone jails
│       └── host-signature.json       # CPU/kernel/Firecracker at capture
│
├── run/                              # (legacy; API socket now lives in the jail)
│
├── pool/
│   └── atlas-pool.img                # sparse loopback PV backing the thin pool
│
├── bootstrap.json                    # host facts written by bootstrap-server.py
│
└── bin/                              # Durable hooks + package laid down by bootstrap
    ├── vm-network-up.py              # ExecStartPre: build the VM's netns + tap + veth
    ├── vm-network-down.py            # ExecStopPost: tear the same down
    ├── vm-disk-up.py                 # ExecStartPre: re-activate the VM's disk LV + refresh its jail node
    ├── vm-restore.py                 # ExecStartPost: resume a pending memory snapshot (no-op on cold boot)
    └── atlas/                        # the durable stdlib-only package the hooks + atlas-pool.service import
        ├── hostinfo.py               # host signature (CPU/kernel/FC) for the warm-restore guard
        ├── lvm.py                    # ThinPool/LogicalVolume (successor to lvm.sh)
        ├── network_env.py            # read network.env, find default route device
        ├── paths.py                  # VirtualMachinePaths, image_directory
        ├── rootfs.py                 # per-VM identity injection (successor to prepare-rootfs.sh)
        ├── _run.py                   # the one subprocess wrapper (set -x + abort-on-fail)
        └── _task.py                  # TaskInputs/TaskResult (typed CLI + ATLAS_RESULT= line)
```

The systemd hooks (`vm-network-up.py`, `vm-network-down.py`, `vm-disk-up.py`,
`vm-restore.py`) take a positional VM uuid (the unit passes `%i`) and add their
own directory to `sys.path` so `import atlas` resolves the package next to
them. They are NOT Tasks — they are excluded from the script catalog so the
runner never executes them as one.

The VM disks themselves are **LVM thin volumes**, not files in this tree —
they live in the `atlas` volume group on the thin pool `pool0`, reachable at
`/dev/atlas/<name>` (base image `atlas-image-<image>`, per-VM disk
`atlas-vm-<uuid>`, disk snapshot `atlas-snap-<uuid>`). A VM with a data disk
adds two more, its peers: the data disk `atlas-data-<uuid>` and its snapshot
`atlas-datasnap-<snapshot-uuid>`. The `pool/atlas-pool.img` sparse file is the
loopback PV that group sits on.

## Conventions

- Mode `0700` on `/var/lib/atlas/` and every immediate subdirectory. Root only.
- One directory per virtual machine, named by UUID. `ls virtual-machines/`
  is the inventory.
- Logs go inside the VM directory, not `/var/log/`. Easier to clean up;
  easier to ship in one tarball.
- The VM's working files — the kernel link, Firecracker config, the API
  socket, and the `rootfs.ext4` block node — live inside the per-VM **jail** at
  `jail/firecracker/<uuid>/root/`, owned by the VM's per-VM uid (the jailer
  chroots Firecracker there). The jail is nested under the VM directory, so
  `rm -rf` of the VM directory still takes everything with it. The disk itself
  is *not* a file here: `rootfs.ext4` is a block-special node `mknod`'d to point
  at the VM's disk LV (`/dev/atlas/atlas-vm-<uuid>`), so the `rm -rf` removes the
  node but not the LV — `terminate-vm.py` `lvremove`s the LV separately. A VM
  with a data disk has a second such node, `data.ext4` → `atlas-data-<uuid>`
  (the guest's `/dev/vdb`), removed the same way on terminate.
- Disk snapshots are **LVM thin snapshots** (`atlas-snap-<snapshot-uuid>`),
  not files under the VM directory. They live in the pool, independent of the
  VM's directory and of the origin VM disk, so terminating a VM does **not**
  take its snapshots with it — `delete-snapshot-vm.py` `lvremove`s the snapshot
  LV explicitly. A snapshot is an instant copy-on-write `lvcreate -s` of the
  VM's disk LV, taken while the VM is Stopped (see [spec/05](./05-virtual-machine-lifecycle.md)).
- The API socket is created by Firecracker inside its jail
  (`jail/.../root/run/firecracker.socket`), not under `/var/lib/atlas/run/`.
  The legacy `run/` directory is still created by bootstrap but is unused.
  Its absolute host path (~150 chars, the UUID nested twice) exceeds the
  108-byte `sun_path` limit for a Unix-domain socket address, so host tools that
  talk to it (`pause-vm.py`, `resume-vm.py`) `cd` into the socket's directory
  and connect via the short relative name `firecracker.socket`. Firecracker
  itself binds it as the relative `run/firecracker.socket` from inside the
  chroot, where the path is short, so the bind never hit the limit.
- Images are read-only after sync. Sync imports the image rootfs into a
  read-only thin LV (`atlas-image-<image>`); provisioning takes an instant
  copy-on-write snapshot of it for the VM's disk. The kernel is still a plain
  file, hard-linked into the jail (one copy, shared by inode).

## Why LVM thin volumes for per-VM disks

Each VM disk is an **LVM thin snapshot of the read-only base image LV** —
`lvcreate -s`, an instant copy-on-write clone that shares the base's blocks
until the VM writes. Not a full `cp`, not overlayfs, not ext4 reflinks (ext4
has none). The base image is itself a read-only thin LV imported at sync.

- **Instant, space-thin provisioning.** A new VM disk is a metadata operation:
  no N-GB copy, near-zero extra space until the guest writes. Density is bounded
  by *written* blocks, not by VM count × image size.
- **CoW snapshots are the same primitive.** A disk snapshot is another
  `lvcreate -s` off the VM's disk LV — instant, shared blocks, and an
  independent origin (removing the base or the VM disk does not break it).
- **Thin-pool origins are independent**, so terminate can `lvremove` a VM disk
  (or even a base image) without checking for snapshots taken from it.
- **Naming derives from UUIDs**, so this needed no DocType/schema change: the VG
  is `atlas`, the pool `pool0`, devices live at `/dev/atlas/atlas-vm-<uuid>`,
  `atlas-snap-<uuid>`, `atlas-image-<image>`. The controller stays path-string
  oriented; the storage model lives in the task scripts and the
  [`atlas.lvm`](../scripts/lib/atlas/lvm.py) module (`ThinPool` / `LogicalVolume`),
  which derive every name from the UUID — the single place the scheme lives.

The PV under the pool depends on the host. On a stock droplet (one disk,
partitioned + mounted as root) there is no spare device, so the PV is a sparse
loopback file (`pool/atlas-pool.img`) on the root disk. On a bare-metal box with
real NVMe (Scaleway Elastic Metal) the PV is a **real device** with no loopback
indirection: either a free whole disk, or — the Scaleway default — the **RAID-1
`data` array** (`/dev/md2`) the install lays down. `ThinPool`'s `PoolBacking`
picks the backing: it honours an explicit `ATLAS_POOL_DEVICE`, else probes
`lsblk` for a raw, unused block device — a whole unpartitioned disk OR a raw
software-RAID array (`discover_pool_disks` recurses into the lsblk tree to reach
md arrays, which are nested under their member partitions) — else falls back to
the loopback file. So the same `bootstrap-server.py` yields a real-device pool on
bare metal and a loopback pool on a droplet with no per-provider branch. The
chosen device set is recorded in `pool/pool-devices` so a reboot re-asserts the
same backing.

### Scaleway disk partitioning

Atlas drives the Scaleway Elastic Metal install with an explicit
`partitioning_schema` rather than the vendor default (which lands boot/root on
disk 0 and leaves the second disk inconsistent, so the pool backing is
non-deterministic). The provider fetches the vendor's *default* schema for the
chosen offer+OS (the source of truth for the box's real device names, which vary
by hardware) and rewrites it into a symmetric, mirrored layout
(`build_raid_partitioning_schema`). Both disks get an **identical, aligned**
partition table — including a `uefi` partition on the second disk that is pure
buffer (only disk 0's ESP is mounted), so the partition numbers line up across
the mirror pair:

| part | size  | RAID array | mount |
|------|-------|------------|-------|
| `uefi` | 512 MiB | — | `/boot/efi` (disk 0 only) |
| `boot` | 1 GiB | `/dev/md0` (raid1) | `/boot` |
| `root` | 64 GiB | `/dev/md1` (raid1) | `/` |
| `data` | rest (`use_all_available_space`) | `/dev/md2` (raid1) | — (raw → LVM PV) |

The `data` RAID (`md2`) is deliberately left out of the install's `filesystems`
— no `mkfs`, no mountpoint — so it is a raw block device that `discover_pool_disks`
picks up as the thin-pool PV. Boot/root arrays carry an ext4 filesystem + a
mountpoint and are correctly skipped by the probe. A box that exposes fewer than
two disks, or an offer/OS that does not support custom partitioning, falls back
to the vendor default install (a free whole disk is then the pool backing as
before). See [spec/08](./08-images.md) for the base-LV import and
[spec/05](./05-virtual-machine-lifecycle.md) for clone/snapshot/resize/terminate
mechanics.

## What if the thin pool runs out of space?

A thin pool over-commits: the sum of VM disk *capacities* can exceed the pool's
real size, and the pool fills as guests actually write. The host-side guard is
pool-space accounting — `snapshot-vm.py` (and any block-allocating op) refuses
when the pool's `data_percent` or `metadata_percent` is ≥90% (read from `lvs`),
rather than `df` on a filesystem. We watch the two percentages **separately**:
metadata exhaustion is the nastier failure (it can wedge the whole pool, not
just one volume), which is why the pool is created with an explicit
`--poolmetadatasize 1G` rather than the auto-formula, which under-sizes for
snapshot-heavy use.

If the pool fills anyway despite the guard, the thin-pool `errorwhenfull`
policy is left at its LVM default: a write that can't allocate **queues for 60s
and then fails with EIO** (rather than `errorwhenfull=y`, which fails
immediately). That gives a monitoring/eviction window a chance to free space
before guests see I/O errors. We don't change this default in this iteration.
Past that, the operator gets paged (out of scope for this iteration), deletes
terminated VMs and stale snapshots, or provisions another server. Pool
autoscale / quota / GC is a spec/09 follow-on; there is no janitor in this
iteration.

## Surviving a host reboot

The pool's backing persists across a reboot, but two pieces of state do not, and
both are reconstructed from on-disk state — never from the Frappe DB:

- **The pool.** The VG/pool activation is gone after a reboot, and a loopback PV
  also loses its loop binding (a real-device PV does not). `atlas-pool.service` (a
  oneshot, ordered `Before=firecracker-vm@.service`) re-runs `ThinPool.ensure()`,
  whose `PoolBacking.reassert()` re-attaches the loop device **only when the
  backing is the file** (reading `pool/pool-devices` to tell the two apart), then
  activates the VG with `vgchange -ay -K`. The `-K` is load-bearing: per-VM disks are
  `lvcreate -s` thin snapshots and carry the LVM **activation-skip** flag, so a
  bare `vgchange -ay` would leave every VM disk inactive. The function is also
  idempotent against LVM's *own* event-based autoactivation, which can surface
  the pool concurrently at boot — it guards the `lvcreate` on a fresh existence
  check rather than aborting.
- **Each VM's disk + jail node.** A VM disk's device-mapper minor can renumber
  across a reboot, which would dangle the `rootfs.ext4` block node mknod'd into
  the jail at provision time. Provision is not re-run on boot, so each
  `firecracker-vm@.service` runs `vm-disk-up.py` as an `ExecStartPre`: it
  re-activates the VM's own disk LV (`-K`) and re-mknods the jail node from the
  LV's *current* major:minor (reading the per-VM uid from `network.env`). This
  is the disk analogue of `vm-network-up.py`, and it makes an enabled VM
  self-heal its disk on every start — reboot, dm-renumber, or a manual
  `lvchange -an` all recover with no operator action.

## Where the Atlas helper scripts come from

The scripts under `/var/lib/atlas/bin/` are the canonical files from
[`atlas/scripts/`](../scripts/), uploaded by `Server.bootstrap()`. When we
edit a script in this repo, re-running Bootstrap on every server pushes the
new copy. The Frappe DB is the source of which version of the script *should*
be there; the file on disk is just a cache of the last bootstrap.

## Atlas-host side: SSH private keys

The Atlas host itself (the machine running the Frappe site) keeps the
SSH private key on disk under `/etc/atlas/keys/atlas.pem` (or whatever
path the operator chose). `Atlas Settings.ssh_private_key_path` stores
the path; the key body is *not* in the DB. The matching public-key body
*is* in the DB at `Atlas Settings.ssh_public_key` — providers that
upload keys at provision time (future Scaleway, AWS) read it from
there, and `Atlas Settings.ssh_key_id` carries the vendor's handle for
providers that need a pre-registered key (DigitalOcean — its key id or
fingerprint).

One Atlas instance, one SSH key. Multi-account ("prod + staging on the
same vendor") is foreclosed by the per-vendor Single Settings model:
stand up a second Atlas site instead.

- Mode `0600` on the key file. Mode `0700` on `/etc/atlas/keys/`.
  Both owned by the Frappe user.
- Atlas reads the file at SSH-connect time via
  `secrets.get_ssh_key_from_disk(path)`. The result is held in memory
  for the duration of the SSH session and not cached.
- Rotating a key is a file-replace operation. There is no UI for
  rotation — the operator overwrites the file (or points
  `ssh_private_key_path` at a new file via a one-off `db.set_value`
  bypass; the field is `set_only_once` for the standard form flow).
- The legacy `ssh_private_key` Password column on the row is migrated
  to disk by `atlas/patches/v1_0/migrate_ssh_key_to_disk.py`. The
  patch is idempotent and writes to disk *before* clearing the DB
  reference, so a partial run is recoverable. The legacy column is
  not dropped — Frappe doesn't drop columns — it just stops being
  read by any controller.
