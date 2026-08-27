#!/usr/bin/env bash
# Experimental thin-LVM setup for metal: a loop-backed thin pool.
# Run as root. For experimentation only.
set -euo pipefail

VG=${VG:-metalvg}
IMG=${IMG:-/var/lib/metal/lvm.img}
SIZE=${SIZE:-2G}

mkdir -p "$(dirname "$IMG")"
[[ -f "$IMG" ]] || truncate -s "$SIZE" "$IMG"

LOOP=$(losetup --find --show "$IMG")
echo "loop: $LOOP"

pvs "$LOOP" >/dev/null 2>&1 || pvcreate "$LOOP"
vgs "$VG" >/dev/null 2>&1 || vgcreate "$VG" "$LOOP"
lvs "$VG/pool" >/dev/null 2>&1 || lvcreate --type thin-pool -l 90%FREE -n pool "$VG"

lvs "$VG"
cat <<EOF

Thin pool "$VG/pool" is ready.

Create a base image (thin LV) and write a rootfs into it:
  lvcreate --thin -V 1G -n base-ubuntu $VG/pool
  dd if=rootfs.ext4 of=/dev/$VG/base-ubuntu bs=4M

metal then does, per VM:
  lvcreate -s -kn -n vm-<id> $VG/base-ubuntu   # thin snapshot (CoW, cheap)
  lvextend -L <sizeM> $VG/vm-<id>              # grow to the requested disk size
  # the guest grows its filesystem on boot (growpart + resize2fs)

Loop devices do not survive reboot. Re-attach with:
  losetup --find --show "$IMG" && vgchange -ay $VG
EOF
