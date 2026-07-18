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

### Two origins for a base image: a URL, or a snapshot (promote)

A base image LV (`atlas-image-<name>`) can be sourced **two** ways, and both end
in the same read-only CoW origin every per-VM disk snapshots from:

1. **From a URL** — `sync-image.py` downloads the Ubuntu cloud image, normalizes
   it, builds the pristine ext4, and `dd`s it into the LV (above). The image row
   carries the kernel/rootfs URLs + checksums.
2. **From a snapshot (promote)** — a baked `Virtual Machine Snapshot` is turned
   into a first-class image so new VMs select it with the ordinary `image` field
   instead of locating a one-off snapshot to *clone*. **Same-server scope only**:
   the bytes never leave the host — snapshot LV → base-image LV is a local `dd`
   (`promote-snapshot-image.py` → `ThinPool.import_base_image_from_lv`), no bucket,
   no SSH copy. The image lives only on the server it was promoted on.

A bake already produces a reusable same-server provisioning source (the snapshot
LV, which `clone_to_new_vm` clones from). Promoting is the small step that makes
that baked artifact a **named image in the picker** rather than a snapshot you
hand-locate. There are exactly two same-server origin shapes a per-VM disk
CoW-snapshots from (`provision-vm.py:_resolve_origin`) — the base-image LV
(`image` field) and a snapshot LV (`clone_to_new_vm`) — and they are the same kind
of CoW origin; promote = snapshot LV → base-image LV.

**The kernel is free.** A baked snapshot was itself provisioned from some base
`Virtual Machine Image` (recorded on the snapshot row as `source_image`), whose
`vmlinux` is already on that server. The promoted image reuses `source_image`'s
`kernel_filename` byte-for-byte (`promote-snapshot-image.py` hard-links it into the
promoted image's directory — same filesystem, no copy); only the **rootfs** LV is
new. Nothing leaves the host, no kernel export.

**Local (URL-less) image rows.** A promoted image has empty
`kernel_url`/`rootfs_url`/`sha256` fields (those are no longer `reqd`, since two
origins now exist) — its bytes are the promoted LV, already on the server, with
nothing to download or verify. `VirtualMachineImage.is_local` is true exactly when
there is no rootfs URL. A local image is **non-syncable**: `after_insert` skips the
sync fan-out and `sync_to_server` throws cleanly (there is nothing for
`sync-image.py` to fetch, and the LV lives on one server). `kernel_filename` and
`rootfs_filename` are still set: `kernel_filename` is the reused kernel;
`rootfs_filename` (`atlas-image-<name>`, the LV name) is the **presence sentinel**
`provision-vm.py`'s step-0 file probe stat's — the real rootfs bytes come from the
base LV via `_resolve_origin`. So a promoted image looks exactly like a synced
image to `provision-vm.py`: a kernel + a rootfs-file probe in
`/var/lib/atlas/images/<name>/`, and a base LV by name.

**Warm snapshots are rejected.** A warm `Virtual Machine Snapshot` is three
artifacts captured at one paused instant — the disk LV, the memory pair
(`vmstate.bin`+`mem.bin`), and `host-signature.json`. Its **value is the resume**:
a warm clone wakes into the frozen, pre-warmed RAM with no grow, no tune2fs reroll,
no disk-mount identity injection (mounting the disk would corrupt the RAM's
filesystem-cache references). A base image is the **opposite contract**: clones
cold-boot and provision *requires* grow + tune2fs + `inject_identity`, none of
which the memory pair survives. Promoting a warm snapshot could only mean
flattening it to its cold disk and discarding the memory pair — throwing away the
one thing that distinguishes it from an ordinary bake. So `promote_to_image()`
**throws on `kind == "Warm"`** before doing any work; promote a *cold* snapshot,
clone the warm one with `clone_to_new_vm`. (Operator decision, 2026-06-19: "Memory
is the real benefit of a warm snapshot. If we lose that, might as well not have
it.")

**Why no transport / no cross-server distribution.** With same-server scope the
bytes never leave the host. Export-to-URL (reusing `sync-image.py`'s download path)
and server-to-server copy are only relevant if fleet-wide distribution is wanted
later; build none of it now.

The two entry points: **`Virtual Machine Snapshot` → Promote to image** (a dialog
taking the image name) and **`Image Build` → Promote to image** (a thin delegate
to the snapshot method with a default name). Both funnel through the one
`promote_to_image` method, so the warm reject and every guard live once.

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
Frappe clone (**plus ERPNext (version-16)** in site mode), MariaDB + Redis (the
bench code and MariaDB datadir on ZFS datasets), nginx + the production stack
configured and **running and serving** — so a snapshot-booted clone comes up
answering on `:80` (IPv4 *and* IPv6) with no deploy step. `build.sh` is the
**proven recipe** ([`../llm/references/bench-setup.md`](../llm/references/bench-setup.md))
and nothing more — its key move is that `bench.toml` sets `process_manager =
"systemd"`, so **bench-cli itself stands up and manages the whole stack** as
lingering `systemctl --user` units that survive reboot. There is no hand-rolled
supervisord unit, ZFS boot drop-in, or nginx surgery; that is all bench-cli's job.

**Two modes** (`build.sh`'s first arg → two golden snapshots): **`site`** bakes a
fully-created Frappe + ERPNext site under the fixed name `site.local`, so a clone's
domain maps to the **site URL** (`deploy-site.py` renames `site.local` → the FQDN via
`bench rename-site`); **`admin`** bakes only the bench + the admin app, so a clone's
domain maps to the **admin URL** (`deploy-site.py` sets `[admin].domain = <fqdn>` +
`bench setup production`). Either way the per-VM deploy is a rename / config-gen,
never a multi-minute `bench new-site` + `install-app erpnext`.

The bake runs as an unprivileged **`frappe` user** (passwordless sudo), not root:
bench-cli's systemd boot persistence is per-user (`loginctl enable-linger frappe` +
enabled `systemctl --user` units), so it needs a real lingering non-root user. The
controller SSHes in as root; `build.sh` runs bench-cli's `install.sh` as root to
create `frappe` (+ its sudoers), then runs every bench step as it.

**MariaDB** is a **dedicated per-bench instance** (`[mariadb].instance = "atlas"`):
`bench init` provisions `mariadb@atlas` with its own socket + datadir and
`systemctl enable --now`s it, so it auto-starts at boot as an ordinary system
service. Atlas never touches MariaDB auth — bench-cli secures it.

**ZFS.** `bench init` creates the pool + dataset from `[volume]` in `bench.toml`
(a preallocated **file vdev**, since the build VM is single-disk) and mounts it,
so BOTH the bench code and the MariaDB data live on ZFS. At the pinned bench-cli the mere presence of a `[volume]` table enables ZFS.
The Firecracker `vmlinux` ships no ZFS module and the cloud squashfs has an empty
`/lib/modules`, so Ubuntu's prebuilt `zfs.ko` (+ its `spl.ko` dep) is baked into the
guest rootfs at **sync time** — `scripts/sync-image.py` `_install_guest_modules` copies
both from the manifest-pinned `linux-modules-<kver>` (the same package/pass that bakes
`virtio_rng`; no DKMS compile or `linux-headers`) and pins `zfs` in `modules-load.d`, so
the module travels in the snapshot and loads on the cold boot that first mounts the pool.
Deriving `kver` from the manifest (not `uname -r`) keeps the module matched to the kernel
the guest actually boots, independent of the build VM. The **one** ZFS thing `build.sh`
does itself is install the `zfsutils-linux` userspace (`zpool`/`zfs`) that bench-cli's
VolumeManager needs. (Cold-boot ZFS auto-import/mount-ordering is not yet wired — to be
verified on a host.)

The golden image is a **`Virtual Machine Snapshot`, not a from-URL
`Virtual Machine Image`** — it is built *inside* a VM and snapshotted, the same
build-in-guest pattern the proxy uses ([12-proxy.md](./12-proxy.md)):

1. Provision a plain `ubuntu-24.04` VM on a server in the region.
2. `atlas.atlas.bench_image.build_bench(<vm>)` uploads the committed
   [`bench/`](../bench/) tree and runs `bench/build.sh` over guest-SSH — the
   sibling of `proxy.build_proxy`. `build.sh` fixes setuid bits, installs the
   `zfsutils-linux` userspace (the `zfs.ko` module is baked at sync time), runs
   bench-cli's `install.sh` (at a pinned commit) as root — which
   creates the `frappe` user + sudoers — then again as `frappe` to install bench-cli,
   drops the committed [`bench/bench.toml`](../bench/bench.toml), and runs
   `bench init` + `bench start` as `frappe`. `bench init` is the heavy,
   per-site-invariant step (the dedicated MariaDB instance + Redis, the ZFS pool,
   the uv venv, the Frappe clone, Node deps, the admin frontend, and — because
   `[production].nginx = true` + `process_manager = "systemd"` — the production
   process units + nginx config + `dns_multitenant = 1`). In **site** mode it then
   installs ERPNext (version-16) and bakes a `site.local` site (`bench new-site` +
   `install-app erpnext`). The stack is left **running and serving** on `:80` over
   both IPv4 and IPv6 (bench-cli emits the v6 listeners).
3. Stop the VM and `Virtual Machine.snapshot(...)` it. bench-cli's lingering
   `systemctl --user` units make a cold boot of the snapshot come up serving
   without a deploy step. That snapshot **is** the golden image; site VMs clone
   from it via `Virtual Machine Snapshot.clone_to_new_vm`.

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
a randomized bake-time throwaway that is **never surfaced**: the owner signs in via
the one-click `login_url` the deploy mints (a real Administrator session — see
[14-self-serve.md](./14-self-serve.md)) and can rotate the password later themselves.
The signup path no longer resets it per VM: that reset cost a full CPU-throttled
`bench frappe` boot (~28s under the 0.25-core cap) that dominated the deploy, and
dropping it is the main latency win (14-self-serve.md).

**Why bake the site, not `bench new-site` per signup.** The slow part of standing
up a Frappe site — `bench new-site`'s schema-create + frappe-install, plus
`install-app erpnext` — is per-site-invariant, so it is paid **once** here at bake
time. A signup's `deploy-site.py` then does only the per-VM work — rename the baked
site to the FQDN via `bench rename-site` — never that multi-minute path, and never a
`set-admin-password` (the owner signs in via the one-click `login_url` the deploy
mints, and may rotate the baked password later themselves);
see [14-self-serve.md](./14-self-serve.md).

**Per-VM identity is the rename (Contract A, site mode).** The bake leaves the
site as `site.local` on disk; the per-VM `deploy-site.py` runs
`bench rename-site site.local <fqdn>`, which moves `sites/site.local` →
`sites/<fqdn>`, rewrites `bench.toml`, removes the stale `site.local.conf`, and
regenerates the bench's vhost with `server_name <fqdn>` (on `listen 80;` +
`listen [::]:80;`, both emitted by bench-cli) so the bench serves the FQDN the proxy
forwards. The on-disk name, the proxy `Host`, and the `Site` key are then **one
string** — no `default_site` / `default_server` indirection. The controller's FQDN
readiness probe runs *after* the rename, so the post-rename `server_name <fqdn>`
vhost matches it directly — there is no pre-rename catch-all to bake. A site VM is
single-tenant (one site per VM). In **admin** mode the equivalent identity step is
setting `[admin].domain = <fqdn>` + `bench setup production`, mapping the FQDN to the
admin app's vhost.

**The bake mode rides snapshot → clone → first boot.** The build VM is stamped with
the recipe's `build_mode` (`site`/`admin`); `Virtual Machine.snapshot()` copies it
onto the golden snapshot, and `clone_to_new_vm` carries it onto the clone — so a
customer VM knows on first boot what its FQDN should map to without the controller
having to remember. The **promote → base-image** path carries it the same way: a
promoted golden's `Virtual Machine Image` row stores the `build_mode`, and a VM
created from it via the ordinary `image` field inherits the mode in `before_insert`
(`set_build_mode_default`, only filling an unset value). So the admin product line
works through the same `image` field the versioned site images use — a VM with
`image = bench-v16-admin` deploys as an admin console without the caller restating the
mode. `deploy_site` reads the clone's `build_mode` and passes
`--mode admin` to the in-guest `deploy-site.py` only when it is admin (site mode is
the unchanged default, no flag). The readiness/health probe is **mode-aware**: a
Frappe site answers `/api/method/ping`, but the admin console is a **Flask** app with
no Frappe ping route — its unauthenticated health endpoint is `/api/status` (200).
Both the controller `wait_for_http` path (`readiness_path_for_mode`) and the in-guest
`_serving` probe pick the right path from the mode, so an admin deploy doesn't report
`serving=false` forever against a 404. The `_admin.conf` vhost bench-cli emits proxies
`location /` (both `listen 80;` and `listen [::]:80;`) to the socket-activated admin
gunicorn, so `/api/status` routes through over the same v6 path the edge proxy uses.

**Versioned variants — V15 / V16 / Nightly.** The bench recipe is **not** a single
golden: three recipes ship — `bench-v15`, `bench-v16`, `bench-nightly` — baked per
Frappe/Bench release and promoted to base images named exactly `bench-v15` /
`bench-v16` / `bench-nightly` (the name Central selects a version by,
[15-image-builder.md](./15-image-builder.md), [16-central.md](./16-central.md)).
They share the **one** committed `bench/` tree and `build.sh`; the only thing that
varies is the per-version pins, which the controller injects without forking the
tree: `bench.toml`'s `[bench].python` + `frappe` `[[apps]].branch` are **rendered**
per recipe before upload, and the bench-cli ref + ERPNext branch ride the build
command's env (`BENCH_CLI_REF` / `ERPNEXT_BRANCH`, which `build.sh` reads as
`${VAR:-default}`). `bench-cli` is the build tool, not the framework — one pinned cli
ref bakes all three (it reads the branch/Python from `bench.toml` and natively knows
`version-15`/`version-16`/`develop`; `uv` fetches the requested interpreter). The
nightly bake stamps the resolved Frappe/ERPNext/cli SHAs into `Image Build.build_inputs`
since `develop` floats. A customer selects the version through the ordinary VM
`image` field (the promoted base image); no new hot-path plumbing. Bumping any pin is
a deliberate update rolled as a new bake + promote, the same discipline
`proxy/build.sh`'s pins follow.

Each version also ships an **admin variant** — `bench-v16-admin`, `bench-v15-admin`,
`bench-nightly-admin` — identical pins but `build_mode = admin`, so the bake skips
the site + ERPNext and produces the admin-console golden described above. They reuse
the same `bench/` tree and version-injection; they differ from their site twins only
in the mode (and so are cold-only, and never register as the self-serve
`default_bench_snapshot`). They promote to base images named `bench-v<NN>-admin` — the
name Central selects them by, alongside the site images.

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
