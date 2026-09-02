#!/usr/bin/env bash
# Experimental ZFS setup for metal: a file-backed zpool.
# Run as root. For experimentation only.
set -euo pipefail

POOL=${POOL:-metal}
IMG=${IMG:-/var/lib/metal/pool.img}
SIZE=${SIZE:-2G}

mkdir -p "$(dirname "$IMG")"
[[ -f "$IMG" ]] || truncate -s "$SIZE" "$IMG"

# zpool create -f -m none <pool> <file>: create the pool on a file vdev; -f
# permits a plain file (not a whole disk) to back the pool; -m none leaves it
# unmounted (only zvols live under it).
zpool list "$POOL" >/dev/null 2>&1 || zpool create -f -m none "$POOL" "$(realpath -m "$IMG")"

# Container datasets. zfs create/clone never make missing parents, so the
# base/<ref> and vms/<id> parents must exist first.
zfs list "$POOL/base" >/dev/null 2>&1 || zfs create -o mountpoint=none "$POOL/base"
zfs list "$POOL/vms" >/dev/null 2>&1 || zfs create -o mountpoint=none "$POOL/vms"

zfs list -r "$POOL"  # show the pool's datasets
cat <<EOF

ZFS pool "$POOL" is ready.

Create a base image (zvol) and write a rootfs into it:
  zfs create -V 1G -o volblocksize=16k $POOL/base/ubuntu   # -V = zvol, 16k blocks
  dd if=rootfs.ext4 of=/dev/zvol/$POOL/base/ubuntu bs=4M   # write the rootfs raw
  zfs snapshot $POOL/base/ubuntu@ready              # the clone source

metal then does, per VM:
  zfs clone $POOL/base/ubuntu@ready $POOL/vms/<id>  # CoW clone, cheap
  zfs set volsize=<sizeM> $POOL/vms/<id>            # grow to the requested disk size
  # the guest grows its filesystem on boot (growpart + resize2fs)

A file-backed pool is not imported automatically after a reboot. Re-attach with:
  zpool import -d "$(dirname "$IMG")" $POOL
EOF
