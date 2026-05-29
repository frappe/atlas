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
  It is a packed, zstd-compressed bzImage; `sync-image.sh` decompresses it to
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
[`scripts/sync-image.sh`](../scripts/sync-image.sh).

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
uncompressed ELF `vmlinux` directly (no bootloader). `sync-image.sh` locates
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
is uploaded to the server alongside `sync-image.sh` before the script runs.
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
arrives. `sync-image.sh` neutralizes that and strips per-VM-shared identity
before building the per-server ext4:

- **cloud-init + boot-blocking services masked.** `cloud-init.service`,
  `cloud-init-local`, `cloud-config`, `cloud-final`,
  `systemd-networkd-wait-online.service`, and snapd
  (`snapd.seeded`/`snapd.service`/`snapd.socket`) are symlinked to
  `/dev/null` so they cannot start; `/etc/cloud/cloud-init.disabled` is also
  set. **Verified on a real Firecracker boot:** without this the guest never
  reaches a login prompt (it spins on `systemd-networkd-wait-online` and
  `snapd.seeded`).
- All `/etc/ssh/ssh_host_*` keypairs removed (otherwise every VM would share
  host keys). Per-VM keys are written at provision time by `provision-vm.sh`;
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
- `/etc/fstab` replaced with a real entry (`LABEL=atlas-root /` plus the
  swapfile from provision-time).
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
swapfile, /etc/hosts 127.0.1.1 line) is written at provision time. See
[05-virtual-machine-lifecycle.md → Guest-side identity contract](./05-virtual-machine-lifecycle.md#guest-side-identity-contract).

### Why we convert squashfs → ext4 server-side

We could pre-build ext4 images on our own bucket. We don't, because:

- We avoid building and storing our own artifacts for the building block.
- The Ubuntu cloud squashfs is public, signed, and stable for a pinned
  dated release.
- Conversion on the server is a few seconds, once per server per image.

When we add custom images (extra packages, custom users), we'll revisit.

## Per-VM rootfs creation

When `provision-vm.sh` runs, it:

1. Copies the pristine ext4 into the VM directory.
2. `truncate -s <disk_gigabytes>G` to grow the file.
3. `e2fsck -fy` + `resize2fs` to extend the filesystem.
4. `mount -o loop` to write `/root/.ssh/authorized_keys`,
   `/etc/atlas-network.env`, `/etc/hostname` + a matching `127.0.1.1`
   line in `/etc/hosts`, a 512 MiB `/swapfile` (referenced by the
   fstab installed at image-sync time), fresh `/etc/ssh/ssh_host_*`
   keypairs (`ssh-keygen` on the host writes directly into the
   mounted rootfs), and a derived `/etc/machine-id`. The
   `atlas-network.service` is already in the pristine image and
   already wanted by `multi-user.target`, so we don't need to touch
   systemd inside the rootfs.
5. `umount`.

This means a freshly booted VM comes up with the right IPv6, the right SSH
key, and a working internet route within ~2 seconds of `systemctl
start`.

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
   `after_insert`, which fans out one `sync-image.sh` Task per `Active`
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
