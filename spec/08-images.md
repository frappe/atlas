# Images

One image for this iteration: **Ubuntu 24.04** from Firecracker CI. The image
is the (kernel, rootfs) pair referenced in the Firecracker getting-started
guide.

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

The canonical values for the supported Ubuntu 24.04 image (URLs,
filenames, SHA-256s) live as a `DEFAULT_IMAGE` constant in
[`atlas/bootstrap.py`](../atlas/bootstrap.py) and
[`atlas/tests/e2e/_config.py`](../atlas/tests/e2e/_config.py). New
operators should copy that dict into the form rather than typing seven
hex-and-URL fields by hand; `atlas.bootstrap.run` inserts the row
directly.

## Sync to a server

One Task per server-image pair, running
[`scripts/sync-image.sh`](../scripts/sync-image.sh).

The script:

1. Ensures the kernel file exists on the server. Downloads and checksums if
   not.
2. Ensures the rootfs ext4 exists. Downloads the source squashfs,
   unsquashes it, drops in `/etc/systemd/system/atlas-network.service` and
   a placeholder `/etc/atlas-network.env`, **normalizes the rootfs** (see
   *Image normalization at sync time* below), and packs the result into an
   ext4 of `default_disk_gigabytes` labelled `atlas-root`. Skips if the
   rootfs is already present.

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

The upstream Firecracker CI rootfs is built for the test harness, not for
end users. `sync-image.sh` strips a fixed set of CI artifacts before
building the per-server ext4:

- `fcnet.service` + `/usr/local/bin/fcnet-setup.sh` (assigns a phantom
  IPv4/30 derived from the MAC — useful for the Firecracker test
  harness, meaningless for us and confusing to a user reading `ip a`).
- All `/etc/ssh/ssh_host_*` keypairs (otherwise every VM would share
  host keys and SSH TOFU would be a lie). Per-VM keys are regenerated
  at provision time by `provision-vm.sh` — the stripped image has no
  reliable first-boot key-regen path (no cloud-init,
  no `ssh-keygen.service`).
- `/etc/machine-id` (cleared at sync time and rewritten per VM at
  provision time, again because the image has no first-boot mechanism
  we can rely on).
- `/etc/hosts` overwritten — the shipped file maps a Docker bridge IP
  to the build-container hostname.
- Root password locked, SSH password-auth disabled (key-only by
  contract). The sshd directive is *prepended* to `sshd_config` rather
  than sed-edited in place — the stripped image often has no
  `PasswordAuthentication` line for sed to match, and a header-prepend
  works either way (first-match-wins in sshd_config).
- `/home/ubuntu` chown'd to uid/gid 1000.
- motd: `50-motd-news` (network nag) and `60-unminimize` (image-is-
  half-baked nag) removed.
- `/etc/fstab` replaced with a real entry (`LABEL=atlas-root /` plus
  the swapfile from provision-time).

If we ever switch to a different upstream rootfs (e.g. real Ubuntu
cloud image), this list becomes the regression-test checklist: each
item should be a no-op on the new image, not a removal.

The per-VM half of the contract (hostname, machine-id, ssh host keys,
swapfile, /etc/hosts 127.0.1.1 line) is written at provision time. See
[05-virtual-machine-lifecycle.md → Guest-side identity contract](./05-virtual-machine-lifecycle.md#guest-side-identity-contract).

### Why we convert squashfs → ext4 server-side

We could pre-build ext4 images on our own bucket. We don't, because:

- We avoid building and storing our own artifacts for the building block.
- The Firecracker CI squashfs is public and stable for the supported
  releases.
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

To roll to a new Ubuntu CI release:

1. Update `kernel_url`, `kernel_sha256`, `rootfs_url`, `rootfs_sha256` on
   the `Virtual Machine Image`.
2. Click **Sync to All Servers**. Each server's Task downloads and rebuilds
   the ext4. The existing ext4 stays put because the script checks for
   filename presence — so old VMs continue to work from the old image bytes
   on the server.

This is intentional: bumping an image does not affect existing VMs. To put
a new VM on the new image, archive and re-provision (changing `image` is
not allowed; see [05-virtual-machine-lifecycle.md](./05-virtual-machine-lifecycle.md)).

If you want the new bits everywhere on a fresh sync, change the
`rootfs_filename` to a new value (e.g. include the release date). Then the
old file remains for old VMs, the new file gets built, and new VMs use it.
