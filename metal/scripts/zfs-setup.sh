#!/usr/bin/env bash
# Experimental ZFS setup for metal: a file-backed zpool.
# Run as root. For experimentation only.
set -euo pipefail

POOL=${POOL:-metal}
IMG=${IMG:-/var/lib/metal/pool.img}
SIZE=${SIZE:-2G}

mkdir -p "$(dirname "$IMG")"
[[ -f "$IMG" ]] || truncate -s "$SIZE" "$IMG"

# A file vdev: zpool uses the image file directly, so no loop device is needed.
zpool list "$POOL" >/dev/null 2>&1 || zpool create -f "$POOL" "$(realpath -m "$IMG")"

zfs list "$POOL"
cat <<EOF

ZFS pool "$POOL" is ready.

Create a base image (zvol) and write a rootfs into it:
  zfs create -V 1G -o volblocksize=16k $POOL/base/ubuntu
  dd if=rootfs.ext4 of=/dev/zvol/$POOL/base/ubuntu bs=4M
  zfs snapshot $POOL/base/ubuntu@ready              # the clone source

metal then does, per VM:
  zfs clone $POOL/base/ubuntu@ready $POOL/vms/<id>  # CoW clone, cheap
  zfs set volsize=<sizeM> $POOL/vms/<id>            # grow to the requested disk size
  # the guest grows its filesystem on boot (growpart + resize2fs)

A file-backed pool is not imported automatically after a reboot. Re-attach with:
  zpool import -d "$(dirname "$IMG")" $POOL
EOF
