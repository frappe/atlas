#!/bin/bash
# Snapshot a Stopped VM's disk: copy its rootfs.ext4 into a snapshot directory.
# Disk-only — no Firecracker memory-state snapshot. The caller guarantees the
# VM is Stopped, so the rootfs is cleanly unmounted and copies consistently.
# Idempotent: re-running overwrites the same snapshot path.
#
# Inputs (environment variables):
#   VIRTUAL_MACHINE_NAME  - UUID; locates the source rootfs
#   SNAPSHOT_ROOTFS_PATH  - absolute destination path for the copied rootfs
#
# Output: prints `SIZE_BYTES=<n>` (the snapshot's byte count) on stdout.

set -euo pipefail

: "${VIRTUAL_MACHINE_NAME:?required}"
: "${SNAPSHOT_ROOTFS_PATH:?required}"

source_rootfs="/var/lib/atlas/virtual-machines/${VIRTUAL_MACHINE_NAME}/rootfs.ext4"
snapshot_directory="$(dirname "$SNAPSHOT_ROOTFS_PATH")"

if [ ! -f "$source_rootfs" ]; then
    echo "rootfs not found for ${VIRTUAL_MACHINE_NAME} (${source_rootfs}); provision the VM first" >&2
    exit 1
fi

# Pre-flight: refuse if free space can't hold the copy (Firecracker docs warn
# unbounded snapshots are a DoS vector). 10% headroom over the source size.
source_bytes="$(sudo stat -c %s "$source_rootfs")"
needed_bytes="$(( source_bytes + source_bytes / 10 ))"
available_kib="$(df --output=avail /var/lib/atlas | tail -1 | tr -d ' ')"
available_bytes="$(( available_kib * 1024 ))"
if [ "$available_bytes" -lt "$needed_bytes" ]; then
    echo "insufficient disk for snapshot: need ${needed_bytes} bytes, ${available_bytes} available on /var/lib/atlas" >&2
    exit 1
fi

sudo install -d -m 0700 "$snapshot_directory"
sudo cp "$source_rootfs" "${SNAPSHOT_ROOTFS_PATH}.part"
sudo mv "${SNAPSHOT_ROOTFS_PATH}.part" "$SNAPSHOT_ROOTFS_PATH"

echo "SIZE_BYTES=$(sudo stat -c %s "$SNAPSHOT_ROOTFS_PATH")"
echo "Snapshotted ${VIRTUAL_MACHINE_NAME} to ${SNAPSHOT_ROOTFS_PATH}."
