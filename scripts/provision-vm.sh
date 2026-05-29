#!/bin/bash
# Provision one Firecracker VM on this server. Single task: prepares disk,
# config, networking, then starts the systemd unit. Run once per VM.
#
# Inputs (environment variables):
#   VIRTUAL_MACHINE_NAME  - UUID, used for directory, tap, systemd instance
#   IMAGE_NAME            - directory under /var/lib/atlas/images
#   KERNEL_FILENAME       - filename inside the image directory
#   ROOTFS_FILENAME       - filename inside the image directory
#   VCPUS                 - integer
#   MEMORY_MB             - integer
#   DISK_GB               - integer, final rootfs size for this VM
#   MAC_ADDRESS           - e.g. 06:00:01:02:03:04
#   TAP_DEVICE            - e.g. atlas-<first 10 chars of vm name>
#   VIRTUAL_MACHINE_IPV6  - the VM's address inside the server's /124
#   IPV4_HOST_CIDR        - host side of the per-VM NAT44 /30, e.g. 100.64.0.9/30
#   IPV4_GUEST_CIDR       - guest side of the same /30, e.g. 100.64.0.10/30
#   IPV4_GATEWAY          - host side address (no mask), the guest's v4 gateway
#   SSH_PUBLIC_KEY        - injected into the rootfs

set -euo pipefail

: "${VIRTUAL_MACHINE_NAME:?required}"
: "${IMAGE_NAME:?required}"
: "${KERNEL_FILENAME:?required}"
: "${ROOTFS_FILENAME:?required}"
: "${VCPUS:?required}"
: "${MEMORY_MB:?required}"
: "${DISK_GB:?required}"
: "${MAC_ADDRESS:?required}"
: "${TAP_DEVICE:?required}"
: "${VIRTUAL_MACHINE_IPV6:?required}"
: "${IPV4_HOST_CIDR:?required}"
: "${IPV4_GUEST_CIDR:?required}"
: "${IPV4_GATEWAY:?required}"
: "${SSH_PUBLIC_KEY:?required}"

# shellcheck source=lib/prepare-rootfs.sh
. "$(dirname "$0")/prepare-rootfs.sh"

image_directory="/var/lib/atlas/images/${IMAGE_NAME}"
vm_directory="/var/lib/atlas/virtual-machines/${VIRTUAL_MACHINE_NAME}"

# 0. Verify image present. Fail loud with an actionable message so the operator
#    knows to click Sync to Server before retrying. (Image sync is multi-minute
#    and is intentionally not auto-triggered from provision.) The kernel is
#    needed regardless of the rootfs source, so this probe stays even when the
#    rootfs comes from a snapshot (clone path, SNAPSHOT_ROOTFS_PATH set).
if [ ! -f "${image_directory}/${ROOTFS_FILENAME}" ]; then
    echo "image '${IMAGE_NAME}' not present on server (missing ${image_directory}/${ROOTFS_FILENAME}); run Sync to Server first" >&2
    exit 1
fi

sudo install -d -m 0700 "$vm_directory"
sudo install -d -m 0700 "${vm_directory}/log"

# 1. Per-VM rootfs. The bytes come from a snapshot copy (clone) when
#    SNAPSHOT_ROOTFS_PATH is set, otherwise from the pristine image. Either way
#    the per-VM identity injected in step 2 is freshly derived from THIS VM's
#    UUID, so a clone never shares host keys or machine-id with its source.
rootfs_path="${vm_directory}/rootfs.ext4"
source_rootfs="${SNAPSHOT_ROOTFS_PATH:-${image_directory}/${ROOTFS_FILENAME}}"
if [ -n "${SNAPSHOT_ROOTFS_PATH:-}" ] && [ ! -f "$SNAPSHOT_ROOTFS_PATH" ]; then
    echo "snapshot rootfs not found: ${SNAPSHOT_ROOTFS_PATH}" >&2
    exit 1
fi
atlas_copy_rootfs "$source_rootfs" "$rootfs_path" "$DISK_GB"

# 2. Inject this VM's identity (SSH key, network env, hostname, swap, host
#    keys, machine-id) into the rootfs. The v4 egress link goes into the
#    guest's network env here too, so clone/rebuild get it for free.
atlas_inject_identity "$rootfs_path" "$VIRTUAL_MACHINE_NAME" "$VIRTUAL_MACHINE_IPV6" \
    "$SSH_PUBLIC_KEY" "$IPV4_GUEST_CIDR" "$IPV4_GATEWAY"

# 3. Firecracker config.
sudo install -m 0644 /dev/stdin "${vm_directory}/firecracker.json" <<EOF
{
  "boot-source": {
    "kernel_image_path": "${image_directory}/${KERNEL_FILENAME}",
    "boot_args": "console=ttyS0 reboot=k panic=1"
  },
  "drives": [
    {
      "drive_id": "rootfs",
      "path_on_host": "${rootfs_path}",
      "is_root_device": true,
      "is_read_only": false
    }
  ],
  "network-interfaces": [
    {
      "iface_id": "eth0",
      "guest_mac": "${MAC_ADDRESS}",
      "host_dev_name": "${TAP_DEVICE}"
    }
  ],
  "machine-config": {
    "vcpu_count": ${VCPUS},
    "mem_size_mib": ${MEMORY_MB}
  }
}
EOF

# 4. Sidecar that vm-network-up.sh reads. Stable across host reboots.
sudo install -m 0644 /dev/stdin "${vm_directory}/network.env" <<EOF
TAP_DEVICE=${TAP_DEVICE}
VIRTUAL_MACHINE_IPV6=${VIRTUAL_MACHINE_IPV6}
IPV4_HOST_CIDR=${IPV4_HOST_CIDR}
EOF

# 5. Enable and start the systemd unit.
sudo systemctl enable --now "firecracker-vm@${VIRTUAL_MACHINE_NAME}.service"

echo "Provisioned ${VIRTUAL_MACHINE_NAME}."
