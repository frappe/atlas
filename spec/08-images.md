# Images

Guest images come from the **Ubuntu cloud-image archive**
(`cloud-images.ubuntu.com`), not from Firecracker CI. Two variants ship for
this iteration, both **Ubuntu 24.04 (noble)**, amd64:

- **server** — `ubuntu-24.04-server-cloudimg-amd64` (the default).
- **minimal** — `ubuntu-24.04-minimal-cloudimg-amd64` (a smaller rootfs).

Each image is a (kernel, rootfs) pair:

- **rootfs**: the upstream `*.squashfs` (converted to ext4 server-side, as
  before).
- **kernel**: the `vmlinuz-generic` from the matching `unpacked/` directory.
  It is a packed, zstd-compressed bzImage; `sync-image.py` decompresses it to
  the uncompressed `vmlinux` Firecracker requires (see *Kernel extraction*).

URLs are pinned to a **dated** release (`release-YYYYMMDD/`), not the floating
`release/` pointer, so the bytes — and therefore the SHA-256 — never change
under us. Server and minimal noble ship the *same* generic kernel (identical
digest).

## Image record

A `Virtual Machine Image` document (see [02-doctypes.md](./02-doctypes.md))
holds:

- URL of the kernel binary.
- URL of the source squashfs rootfs.
- SHA-256 of each.
- Filenames the server uses to store them.
- A `default_disk_gigabytes` used when a VM doesn't override it.

Image bytes never live in the Frappe DB. They live as files on each server
and as a URL anywhere else.

The canonical values for both supported images (URLs, filenames,
SHA-256s) live as `DEFAULT_IMAGE` (server) and `MINIMAL_IMAGE` (minimal)
constants in [`atlas/bootstrap.py`](../atlas/bootstrap.py) and
[`atlas/tests/e2e/_config.py`](../atlas/tests/e2e/_config.py). New
operators should copy a dict into the form rather than typing seven
hex-and-URL fields by hand; `atlas.bootstrap.run` inserts the server row
directly. `kernel_sha256` is the digest of the *downloaded packed*
`vmlinuz` (matching upstream `SHA256SUMS`); the extracted `vmlinux` is a
derived artifact and is not separately pinned.

## Sync to a server

One Task per server-image pair, running
[`scripts/sync-image.py`](../scripts/sync-image.py).

Sync is **automatic** on image creation:
`Virtual Machine Image.after_insert` enumerates every `Server` with
`status = Active` and calls `self.sync_to_server(server)` for each.
The operator only saves the image; the fan-out enqueues one Task per
target. New `Active` servers added later are caught up via the same
`sync_to_server` method, invoked from the Server form's **Sync Image**
Actions item (a one-field dialog picking an `is_active = 1` image) or
from the e2e harness — there is no operator-facing button on the
Image form for ad-hoc per-server sync.

The image row itself is immutable from insert. Every non-`is_active`
field carries `set_only_once`, and `_validate_immutability` raises if
a backdoor write tries to mutate kernel/rootfs URLs or checksums.
Rotating an image means inserting a new row (which auto-syncs) and
archiving the old one via the `archive()` controller method.

The script:

1. Ensures the kernel file exists on the server. Downloads the packed
   `vmlinuz`, checksums it against `kernel_sha256`, then **decompresses the
   zstd payload to an uncompressed `vmlinux`** (see *Kernel extraction*).
   Skips if the final `vmlinux` is already present.
2. Ensures the rootfs ext4 exists. Downloads the source squashfs,
   unsquashes it, drops in `/etc/systemd/system/atlas-network.service` and
   a placeholder `/etc/atlas-network.env`, **normalizes the rootfs** (see
   *Image normalization at sync time* below), and packs the result into an
   ext4 of `default_disk_gigabytes` labelled `atlas-root`. Skips if the
   rootfs is already present.

### Kernel extraction

The Ubuntu cloud kernel ships as a packed **PE/EFI bzImage** whose payload is
a **zstd frame followed by a 4-byte size trailer**; Firecracker boots an
uncompressed ELF `vmlinux` directly (no bootloader). `sync-image.py` locates
the zstd magic (`28 b5 2f fd`) inside the bzImage, decompresses from that
offset with **`zstd -dc -f`**, and verifies the result starts with the ELF
magic (`7f 45 4c 46`).

The `-f` (force) flag is load-bearing: plain `unzstd` / `zstd -d` reject the
stream as "unsupported format" because of the trailing size bytes after the
frame; `-f` decompresses the valid frame and ignores the trailer.

We deliberately do **not** use the kernel.org `extract-vmlinux` helper: it
verifies the result with `readelf` (not installed on a stock Firecracker host),
so it silently yields a 0-byte file. The direct magic-scan + `zstd -dc -f` +
ELF-check is host-tool-independent (`xxd`, `zstd` only). Verified booting on a
real Firecracker host.

The guest unit file [`scripts/guest/atlas-network.service`](../scripts/guest/atlas-network.service)
is uploaded to the server alongside `sync-image.py` before the script runs.
The script's `GUEST_NETWORK_UNIT` env var points at it. The upload is
declared via the `SCRIPT_UPLOADS` map in
[`atlas/atlas/script_uploads.py`](../atlas/atlas/script_uploads.py); the
general mechanism (any script can declare sidecar uploads, picked up by
`run_task`) is described in
[04-tasks.md → Sidecar uploads](./04-tasks.md#sidecar-uploads-script_uploads).
Keeping the unit file as a real file (not a heredoc inside the script) means
we can lint it, diff it, and edit it without touching shell code.

### Image normalization at sync time

The Ubuntu cloud image is built for a generic cloud with a metadata
datasource and a first-boot agent (cloud-init) — neither of which exists in
Atlas's model (static IPv6 brought up by `atlas-network.service`, identity
injected by mounting the rootfs at provision time). Left untouched it would
**hang boot forever** waiting on a datasource and a network that never
arrives. `sync-image.py` neutralizes that and strips per-VM-shared identity
before building the per-server ext4:

- **cloud-init + boot-blocking services masked.** `cloud-init.service`,
  `cloud-init-local`, `cloud-config`, `cloud-final`,
  `systemd-networkd-wait-online.service`, and snapd
  (`snapd.seeded`/`snapd.service`/`snapd.socket`) are symlinked to
  `/dev/null` so they cannot start; `/etc/cloud/cloud-init.disabled` is also
  set. **Verified on a real Firecracker boot:** without this the guest never
  reaches a login prompt (it spins on `systemd-networkd-wait-online` and
  `snapd.seeded`).
- **Boot-speed junk masked (`_JUNK_UNITS`).** `apport*`, `ModemManager`,
  `multipathd`, `udisks2`, `polkit`, `lxd-installer.socket`, and the snapd
  *leaf* units the core mask above misses (`snapd.apparmor`, `snapd.autoimport`,
  `snapd.core-fixup`, …) are masked too. Unlike the boot-blockers, **none of
  these gate boot** — they run in parallel — so masking them is a speed/hygiene
  win, not a correctness fix: it removes the off-path boot-storm work (apport
  ~17s, ModemManager ~9s, measured) that slows the units which *do* gate sshd.
  `mariadb`/`redis` are deliberately left enabled (a site VM needs them). This
  does **not** approach a ~1s boot on its own: the dominant *serial* gates are
  `apparmor.service` (~10s) and the virtio `dev-vda`/tmpfiles chain — the next
  levers, deliberately untouched here.
- All `/etc/ssh/ssh_host_*` keypairs removed (otherwise every VM would share
  host keys). Per-VM keys are written at provision time by `provision-vm.py`;
  we do not rely on first-boot regeneration (cloud-init is masked).
- `/etc/machine-id` cleared at sync time and rewritten per VM at provision
  time.
- `/etc/hosts` overwritten with a minimal template (Atlas owns it; per-VM
  `127.0.1.1` line added at provision time).
- Root password locked, SSH password-auth disabled (key-only by contract).
  The cloud image's `sshd_config` has `Include sshd_config.d/*.conf` and
  ships `60-cloudimg-settings.conf` enabling password auth, so Atlas drops a
  lexically-first `sshd_config.d/00-atlas.conf` — it wins by first-match
  rather than relying on prepend ordering against the Include.
- `/home/ubuntu` chown'd to uid/gid 1000 **only if it exists** — the cloud
  image does *not* ship it (cloud-init would create the `ubuntu` user on first
  boot, which we've masked). Atlas SSHes in as root, so the `ubuntu` user is
  irrelevant; this is a guarded no-op on the cloud image.
- motd: `50-motd-news` and `60-unminimize` removed (no-op if absent).
- `/etc/fstab` replaced with a real entry (`LABEL=atlas-root /`).
- `fcnet.service` + `/usr/local/bin/fcnet-setup.sh` removed. **No-op on the
  Ubuntu cloud image** (those are Firecracker-CI artifacts). Kept as harmless
  `rm -f` calls so the step documents the contract and survives a future
  image that does ship them.

This list is the **regression-test checklist** for any upstream rootfs swap:
each item must be a no-op or a correct strip on the new image, never silently
dropped. The cloud-init/networkd/snapd masks are the load-bearing items for
*this* image; the fcnet removal is the load-bearing item for the old CI image
and now a documented no-op.

The per-VM half of the contract (hostname, machine-id, ssh host keys,
/etc/hosts 127.0.1.1 line) is written at provision time. See
[05-virtual-machine-lifecycle.md → Guest-side identity contract](./05-virtual-machine-lifecycle.md#guest-side-identity-contract).

### Why we convert squashfs → ext4 server-side

We could pre-build ext4 images on our own bucket. We don't, because:

- We avoid building and storing our own artifacts for the building block.
- The Ubuntu cloud squashfs is public, signed, and stable for a pinned
  dated release.
- Conversion on the server is a few seconds, once per server per image.

When we add custom images (extra packages, custom users), we'll revisit.

### Base image as a read-only thin LV

After the pristine ext4 file is built, `sync-image.py` also imports it into a
**read-only LVM thin volume** named `atlas-image-<image_name>` (in the `atlas`
volume group on thin pool `pool0`): a thin LV of `DEFAULT_DISK_GB` is created,
the ext4 bytes are `dd`'d in, and the LV is flipped read-only (`lvchange
--permission r`). The LV is sized to the full disk so its free space lands in
the base and every per-VM snapshot inherits it without a per-VM `lvextend`.

This is what makes per-VM disk creation **instant**: instead of copying the
whole ext4, `provision-vm.py` takes a CoW thin snapshot of this base LV
(`lvcreate -s`), which shares all unwritten base blocks (see
[05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md) and
[07-filesystem-layout.md](./07-filesystem-layout.md)). The on-disk ext4 file
is kept too — it is the import source (re-`dd`'d into the LV only if the LV is
absent, so a re-sync of an unchanged image is a no-op) and the audit artifact.
The LV name is derived from `image_name`, so there is no DocType field for it.

## Per-VM rootfs creation

When `provision-vm.py` runs, it:

1. Creates the VM's disk LV `atlas-vm-<uuid>` as an **instant CoW thin
   snapshot** (`lvcreate -s`) of the origin LV — the base image LV
   `atlas-image-<image>` normally, or a snapshot LV when cloning. No full
   copy: unwritten blocks are shared with the origin.
2. `lvextend -r` to `<disk_gigabytes>G` if the VM's disk is larger than the
   origin (one shot — grows the LV and the ext4 on it together).
3. `e2fsck -fy`, then `tune2fs -U random -L atlas-root` to give this disk a
   distinct ext4 UUID. A CoW snapshot inherits the origin's ext4 UUID;
   `mount -o nouuid` is XFS-only (not ext4), so without this the host's
   `blkid` would see duplicate UUIDs. The guest mounts `root=/dev/vda`, so it
   is UUID-agnostic — this is purely host-side hygiene, done while unmounted.
4. `mount` the LV **device** (no `-o loop` — it is a real block device) to
   write `/root/.ssh/authorized_keys`, `/etc/atlas-network.env`,
   `/etc/hostname` + a matching `127.0.1.1` line in `/etc/hosts`, fresh
   `/etc/ssh/ssh_host_*` keypairs, and a derived `/etc/machine-id`. The
   `atlas-network.service` is already in the pristine image and already wanted
   by `multi-user.target`, so we don't touch systemd inside the rootfs.
5. `umount`.
6. `mknod` the LV's block device into the jail as `rootfs.ext4`, owned by the
   per-VM uid (`0660`), so the chrooted, de-privileged Firecracker can open it
   via pure DAC. `firecracker.json`'s jail-relative `path_on_host:
   "rootfs.ext4"` is unchanged — it is now a block node rather than a file.
   See [07-filesystem-layout.md](./07-filesystem-layout.md) for the LVM ×
   jailer details.

This means a freshly booted VM comes up with the right IPv6, the right SSH
key, and a working internet route within ~2 seconds of `systemctl start` —
and disk creation is **instant** (a CoW snapshot, not a multi-second copy).

## The golden bench image (self-serve)

Self-serve site VMs don't boot the plain `ubuntu-24.04` image — they boot a
**golden bench image**: the same Ubuntu rootfs with bench-cli, its uv venv, the
Frappe clone **plus ERPNext (version-16)**, MariaDB + Redis on a **mandatory ZFS
pool**, nginx + supervisor configured and enabled, **a fully-created Frappe +
ERPNext site baked under the fixed name `site.local`**, and the production stack
left **running and serving** — so a snapshot-booted clone comes up answering on
`:80` (IPv4 *and* IPv6) with no deploy step. Baking all of that once means
`deploy-site.py` (the in-guest deploy) does only the per-VM work — **rename** the
baked `site.local` dir to the FQDN + `bench setup nginx` (regenerate the vhost as
`server_name <fqdn>` + reload), no admin reset (cold clones also `setup production`
first) — not a multi-minute `bench new-site` + `install-app erpnext` per signup.

**ZFS is mandatory** in the current bench-cli: `bench init` sets up a ZFS pool
unconditionally on Linux. The build VM is a single-disk droplet, so `bench.toml`
configures a **file-backed** pool (`[volume] backing = "image"`, a preallocated
vdev on the root filesystem); `init` mounts the `benches` dataset at
`/root/bench-cli/benches` and the `mariadb` dataset at `/var/lib/mysql`. Because
the bench code *and* the MariaDB data live on ZFS, `build.sh` orders MariaDB and
the bench-owned supervisord `After=` the ZFS mount, and runs that supervisord as
a systemd unit (`atlas-bench.service`) so a cold boot of the snapshot brings the
stack up unattended — bench-cli's supervisord is otherwise launched by hand by
`bench start`, with no boot persistence.

The golden image is a **`Virtual Machine Snapshot`, not a from-URL
`Virtual Machine Image`** — it is built *inside* a VM and snapshotted, the same
build-in-guest pattern the proxy uses ([12-proxy.md](./12-proxy.md)):

1. Provision a plain `ubuntu-24.04` VM on a server in the region.
2. `atlas.atlas.bench_image.build_bench(<vm>)` uploads the committed
   [`bench/`](../bench/) tree and runs `bench/build.sh` over guest-SSH — the
   sibling of `proxy.build_proxy`. `build.sh` installs bench-cli at a pinned
   commit, drops the committed [`bench/bench.toml`](../bench/bench.toml), and
   runs `bench init` (the heavy, per-site-invariant step: apt MariaDB + Redis,
   the mandatory ZFS pool, uv venv, Frappe clone, Node + npm deps, admin
   frontend, and — because `[production].nginx = true` — the supervisor + nginx
   bring-up). It then installs **ERPNext (version-16)**, bakes a `site.local`
   site (`bench new-site` + `install-app erpnext`, past the setup-wizard gate),
   runs `bench setup production` (adding IPv6 listeners and the boot-ordering
   units), and leaves the production stack **running and serving** the baked site
   on `:80` over both IPv4 and IPv6.
3. Stop the VM and `Virtual Machine.snapshot(...)` it. The systemd boot wiring
   (`atlas-bench.service`, ZFS-ordered MariaDB/nginx) makes a cold boot of the
   snapshot come up serving without a deploy step. That snapshot **is** the
   golden image; site VMs clone from it via
   `Virtual Machine Snapshot.clone_to_new_vm`.

We deliberately do **not** chroot-bake the rootfs at sync time: apt's
MariaDB/Redis postinst maintainer scripts expect a running init, which a bare
chroot lacks. Building in a real booted guest sidesteps that and reuses the
existing snapshot machinery for the rollable artifact. The bake is driven by the
[`bench_image`](../atlas/tests/e2e/use_cases/bench_image.py) operator action
(provision → build → stop → snapshot → assert `bench` runs over guest-SSH).

**The db secret.** `bench.toml` carries a fixed, baked MariaDB `root_password`
shared across every VM from this image — correct because each VM is
single-tenant and MariaDB binds localhost only (the south hop reaches Frappe's
`:80`, never `3306`). The Frappe Administrator password is **also baked + shared** —
a throwaway the owner is handed and rotates after first login. The signup path no
longer resets it per VM: that reset cost a full CPU-throttled `bench frappe` boot
(~28s under the 0.25-core cap) that dominated the deploy, and dropping it is the
main latency win (14-self-serve.md). Lazy per-site rotation (first login / a job)
is deferred.

**Why bake the site, not `bench new-site` per signup.** The slow part of standing
up a Frappe site — `bench new-site`'s schema-create + frappe-install, plus
`install-app erpnext` — is per-site-invariant, so it is paid **once** here at bake
time. A signup's `deploy-site.py` then does only the per-VM work — rename the baked
dir to the FQDN + `bench setup nginx` — never that multi-minute path, and never a
`set-admin-password` (the owner is handed the shared baked password and rotates it);
see [14-self-serve.md](./14-self-serve.md).

**Per-VM identity is the rename (Contract A).** The bake leaves the site as
`site.local` on disk; the per-VM `deploy-site.py` renames `sites/site.local` →
`sites/<fqdn>` and runs `bench setup nginx`, which regenerates the bench's vhost
with `server_name <fqdn>` so the bench serves the FQDN the proxy forwards. The
on-disk name, the proxy `Host`, and the `Site` key are then **one string** — there
is no `default_site`/`default_server` indirection. The bake still marks the vhost
`default_server` so a **pre-rename** probe (the warm resume, before the deploy runs)
answers off the baked `site.local`; the deploy's `bench setup nginx` replaces that
with the `server_name <fqdn>` vhost. A site VM is single-tenant (one site per VM).

Versions are pinned (bench-cli commit in `build.sh`, Frappe branch in
`bench.toml`); bumping any is a deliberate update rolled as a new golden
snapshot, the same discipline `proxy/build.sh`'s pins follow.

## Verification

Every download is checksummed against the value on the image record.
Mismatch is a hard failure of the Task. The `.part` temp file is left in
place for inspection.

## Bumping an image

Image rows are immutable after insert. To roll to a newer Ubuntu cloud
release (a later dated `release-YYYYMMDD/`), **create a new
`Virtual Machine Image` row** and archive the old one:

1. Insert a new `Virtual Machine Image` with a distinct `image_name`
   (e.g. include the release date or the upstream tag), the new URLs,
   and the new SHA-256 digests. Saving the row triggers
   `after_insert`, which fans out one `sync-image.py` Task per `Active`
   Server automatically.
2. On the old row, run **Archive** under `Actions ▾`. This flips
   `is_active = 0` and removes it from the image picker on new VM
   forms. The on-disk kernel + rootfs are *not* deleted — VMs already
   provisioned from the old image keep working from their per-VM ext4
   copy, and the per-server kernel + rootfs files survive until the
   operator cleans them up by hand.

Bumping an image does not affect existing VMs: per-VM rootfs files are
full copies, not overlays, so the image's bytes on the server are
irrelevant once a VM is provisioned. Changing `image` on a VM row is
forbidden (`_validate_immutability`); see
[05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md). To
move a VM onto the new image, terminate it and re-provision against the
new row.

The old contract — "edit the image's URLs + checksums in place, then
click Sync to All Servers" — is gone. Editing in place would silently
invalidate any audit row that referenced the old digest, so the
controller now refuses kernel/rootfs URL or SHA-256 mutations
post-insert.
