#!/bin/bash
# Rebuild/Restore a Stopped VM's disk from a source, keeping its identity
# (name, IPv6, MAC, tap, SSH key). The source is either one of the VM's own
# snapshots (Restore) or a base image's pristine rootfs (Rebuild). Either way
# the VM keeps its UUID, so step 2's freshly-derived host keys / machine-id /
# hostname match the VM the operator already knows.
#
# The caller guarantees the VM is Stopped (the unit is down, rootfs unmounted),
# so swapping the file underneath it is safe. firecracker.json, network.env and
# the systemd unit already exist from the original provision and are untouched.
# Idempotent: re-running replaces the rootfs again with the same source.
#
# Inputs (environment variables):
#   VIRTUAL_MACHINE_NAME  - UUID; locates the VM directory and seeds identity
#   DISK_GB               - target rootfs size (the VM's current disk size)
#   VIRTUAL_MACHINE_IPV6  - injected into the rootfs network env
#   IPV4_GUEST_CIDR       - guest side of the NAT44 /30, injected into the env
#   IPV4_GATEWAY          - host side of the /30 (no mask), the guest's v4 gw
#   SSH_PUBLIC_KEY        - injected into authorized_keys
#   ATLAS_FC_UID          - per-VM uid; the rebuilt rootfs is chowned back to it
#   One source, exactly:
#     SNAPSHOT_ROOTFS_PATH  - absolute path to a snapshot rootfs (Restore), OR
#     IMAGE_NAME + ROOTFS_FILENAME - a base image under /var/lib/atlas/images (Rebuild)

set -euo pipefail

# shellcheck source=lib/prepare-rootfs.sh
. "$(dirname "$0")/prepare-rootfs.sh"

: "${VIRTUAL_MACHINE_NAME:?required}"
: "${DISK_GB:?required}"
: "${VIRTUAL_MACHINE_IPV6:?required}"
: "${IPV4_GUEST_CIDR:?required}"
: "${IPV4_GATEWAY:?required}"
: "${SSH_PUBLIC_KEY:?required}"
: "${ATLAS_FC_UID:?required}"

vm_directory="/var/lib/atlas/virtual-machines/${VIRTUAL_MACHINE_NAME}"
# The rootfs lives inside the jail (built at provision); rebuild swaps that file.
jail_root="${vm_directory}/jail/firecracker/${VIRTUAL_MACHINE_NAME}/root"
rootfs_path="${jail_root}/rootfs.ext4"

if [ ! -d "$jail_root" ]; then
    echo "jail ${jail_root} missing; provision the VM before rebuilding" >&2
    exit 1
fi

# Resolve the source rootfs. Snapshot path wins; otherwise the base image.
if [ -n "${SNAPSHOT_ROOTFS_PATH:-}" ]; then
    source_rootfs="$SNAPSHOT_ROOTFS_PATH"
    if [ ! -f "$source_rootfs" ]; then
        echo "snapshot rootfs not found: ${source_rootfs}" >&2
        exit 1
    fi
else
    : "${IMAGE_NAME:?required (or pass SNAPSHOT_ROOTFS_PATH)}"
    : "${ROOTFS_FILENAME:?required (or pass SNAPSHOT_ROOTFS_PATH)}"
    source_rootfs="/var/lib/atlas/images/${IMAGE_NAME}/${ROOTFS_FILENAME}"
    if [ ! -f "$source_rootfs" ]; then
        echo "image rootfs not present: ${source_rootfs}; run Sync to Server first" >&2
        exit 1
    fi
fi

# Replace the existing rootfs. atlas_copy_rootfs no-ops when the dest exists,
# so remove it first to force the swap.
sudo rm -f "$rootfs_path"
atlas_copy_rootfs "$source_rootfs" "$rootfs_path" "$DISK_GB"
atlas_inject_identity "$rootfs_path" "$VIRTUAL_MACHINE_NAME" "$VIRTUAL_MACHINE_IPV6" \
    "$SSH_PUBLIC_KEY" "$IPV4_GUEST_CIDR" "$IPV4_GATEWAY"

# The new rootfs was created by root (cp); hand it back to the per-VM uid so the
# jailed Firecracker can open it RW.
sudo chown "${ATLAS_FC_UID}:${ATLAS_FC_UID}" "$rootfs_path"

echo "Rebuilt ${VIRTUAL_MACHINE_NAME} from ${source_rootfs}."
