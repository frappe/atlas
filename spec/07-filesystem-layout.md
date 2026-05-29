# Filesystem layout on the server

Everything Atlas puts on a server lives under `/var/lib/atlas/`. Nothing
else.

```
/var/lib/atlas/
├── images/
│   └── ubuntu-24.04/
│       ├── vmlinux-6.1.141           # kernel binary, immutable per image
│       └── ubuntu-24.04.ext4         # pristine rootfs, immutable per image
│
├── virtual-machines/
│   ├── d4f7c1a2-7e0a-4f1b-93cc-ad96b9b39b3e/
│   │   ├── firecracker.json          # Firecracker --config-file
│   │   ├── rootfs.ext4               # per-VM mutable rootfs
│   │   ├── network.env               # TAP_DEVICE, VIRTUAL_MACHINE_IPV6, IPV4_HOST_CIDR
│   │   └── log/
│   │       └── firecracker.log
│   ├── 19ae...                       # one directory per VM, named by UUID
│   └── ...
│
├── run/
│   ├── d4f7c1a2-...-9b3e.sock        # Firecracker API socket per VM
│   ├── 19ae...-.sock
│   └── ...
│
└── bin/                              # Helper scripts laid down by bootstrap
    ├── vm-network-up.sh
    └── vm-network-down.sh
```

## Conventions

- Mode `0700` on `/var/lib/atlas/` and every immediate subdirectory. Root only.
- One directory per virtual machine, named by UUID. `ls virtual-machines/`
  is the inventory.
- Logs go inside the VM directory, not `/var/log/`. Easier to clean up;
  easier to ship in one tarball.
- API sockets live under `/var/lib/atlas/run/`, not `/var/run/firecracker/`.
  We do not share the path with anything else.
- Images are read-only after sync. Provisioning copies the image rootfs into
  the VM directory.

## Why plain `cp` for per-VM rootfs

This iteration uses a full copy of the image rootfs per VM. Not overlayfs,
not CoW, not reflinks.

- Simple to reason about (one regular file per VM).
- Survives reboots and corruption without special handling.
- Disk overhead is bounded: ~600 MB pristine, resized up to `disk_gigabytes`.
  On `s-2vcpu-4gb-intel` (80 GB SSD) that's room for many VMs.

When density matters we will revisit. The decision is reversible — switch
to overlayfs by changing one script, no doc-type changes needed.

## What if `/var/lib/atlas/` runs out of space?

The operator gets paged (out of scope for this iteration), runs `df`, deletes
terminated VMs (they should already be cleaned by `terminate-vm.sh`), or
provisions another server. No janitor in this iteration.

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
there, and `Atlas Settings.ssh_fingerprint` carries the vendor-side
reference for providers that need a pre-registered fingerprint
(DigitalOcean).

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
