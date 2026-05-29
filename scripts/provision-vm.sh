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
#   TAP_DEVICE            - e.g. atlas-<first 9 hex of vm name>
#   VIRTUAL_MACHINE_IPV6  - the VM's address inside the server's /124
#   IPV4_HOST_CIDR        - host side of the per-VM NAT44 /30, e.g. 100.64.0.9/30
#   IPV4_GUEST_CIDR       - guest side of the same /30, e.g. 100.64.0.10/30
#   IPV4_GATEWAY          - host side address (no mask), the guest's v4 gateway
#   SSH_PUBLIC_KEY        - injected into the rootfs
#   ATLAS_FC_UID          - per-VM uid the jailer drops Firecracker to (gid == uid)
#   ATLAS_NETNS           - per-VM network namespace name
#   HOST_VETH             - host-side veth interface name
#   NAMESPACE_VETH        - namespace-side veth interface name
#   ATLAS_CGROUP_ARGS     - jailer --cgroup flags (space-separated)
#   ATLAS_RESOURCE_ARGS   - jailer --resource-limit flags (space-separated)

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
: "${ATLAS_FC_UID:?required}"
: "${ATLAS_NETNS:?required}"
: "${HOST_VETH:?required}"
: "${NAMESPACE_VETH:?required}"
: "${ATLAS_CGROUP_ARGS:?required}"
: "${ATLAS_RESOURCE_ARGS:?required}"

# shellcheck source=lib/prepare-rootfs.sh
. "$(dirname "$0")/prepare-rootfs.sh"

image_directory="/var/lib/atlas/images/${IMAGE_NAME}"
vm_directory="/var/lib/atlas/virtual-machines/${VIRTUAL_MACHINE_NAME}"
# The jailer chroots Firecracker into <chroot-base>/firecracker/<id>/root. We
# point the chroot base at the VM directory so everything Atlas writes still
# lives under /var/lib/atlas. The per-VM rootfs, kernel, config and API socket
# all live inside this jail root, owned by the per-VM uid.
jail_root="${vm_directory}/jail/firecracker/${VIRTUAL_MACHINE_NAME}/root"

# 0. Verify image present. Fail loud with an actionable message so the operator
#    knows to click Sync to Server before retrying. (Image sync is multi-minute
#    and is intentionally not auto-triggered from provision.) The kernel is
#    needed regardless of the rootfs source, so this probe stays even when the
#    rootfs comes from a snapshot (clone path, SNAPSHOT_ROOTFS_PATH set).
if [ ! -f "${image_directory}/${ROOTFS_FILENAME}" ]; then
    echo "image '${IMAGE_NAME}' not present on server (missing ${image_directory}/${ROOTFS_FILENAME}); run Sync to Server first" >&2
    exit 1
fi

# 0b. Per-VM uid collision guard. The uid is derived from the UUID and is almost
#     always unique, but a mod collision is possible. If a *different* live VM's
#     jail rootfs is already owned by this uid, fail loud rather than silently
#     letting two VMs share a uid (which would break inter-jail isolation).
for other_jail in /var/lib/atlas/virtual-machines/*/jail/firecracker/*/root/rootfs.ext4; do
    [ -e "$other_jail" ] || continue
    case "$other_jail" in
        "${jail_root}/rootfs.ext4") continue ;;  # our own (idempotent re-run)
    esac
    if [ "$(sudo stat -c '%u' "$other_jail")" = "$ATLAS_FC_UID" ]; then
        echo "uid ${ATLAS_FC_UID} already owned by ${other_jail}; uid collision — terminate that VM or re-roll" >&2
        exit 1
    fi
done

sudo install -d -m 0700 "$vm_directory"
sudo install -d -m 0700 "${vm_directory}/log"
sudo install -d -m 0700 "${jail_root}"
sudo install -d -m 0700 "${jail_root}/run"

# 1. Per-VM rootfs inside the jail. The bytes come from a snapshot copy (clone)
#    when SNAPSHOT_ROOTFS_PATH is set, otherwise from the pristine image. Either
#    way the per-VM identity injected in step 2 is freshly derived from THIS VM's
#    UUID, so a clone never shares host keys or machine-id with its source.
rootfs_path="${jail_root}/rootfs.ext4"
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

# 3. Kernel inside the jail. Hard-link (not copy) the immutable image kernel so
#    we don't duplicate it per VM; same filesystem (/var/lib/atlas), so the link
#    always succeeds. Read-only is fine for the jailed process.
sudo ln -f "${image_directory}/${KERNEL_FILENAME}" "${jail_root}/vmlinux"

# 4. Firecracker config inside the jail, with jail-RELATIVE paths — they are
#    resolved by the jailed process after chroot, so they are relative to the
#    jail root (/rootfs.ext4, /vmlinux), not absolute host paths.
sudo install -m 0644 /dev/stdin "${jail_root}/firecracker.json" <<EOF
{
  "boot-source": {
    "kernel_image_path": "vmlinux",
    "boot_args": "console=ttyS0 reboot=k panic=1"
  },
  "drives": [
    {
      "drive_id": "rootfs",
      "path_on_host": "rootfs.ext4",
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

# 5. Hand the jail tree to the per-VM uid/gid. The jailer also chowns the jail
#    root and the device nodes it creates, but the backing files we laid down
#    (rootfs RW, kernel RO, config) must be owned by the uid too. Do this last,
#    after every file is in place.
sudo chown -R "${ATLAS_FC_UID}:${ATLAS_FC_UID}" "${vm_directory}/jail"

# 6. Sidecar that vm-network-up.sh reads. Stable across host reboots — carries
#    the tap, address, and the per-VM netns + veth names so networking is
#    reconstructible after a host reboot without consulting the Frappe DB.
sudo install -m 0644 /dev/stdin "${vm_directory}/network.env" <<EOF
TAP_DEVICE=${TAP_DEVICE}
VIRTUAL_MACHINE_IPV6=${VIRTUAL_MACHINE_IPV6}
ATLAS_NETNS=${ATLAS_NETNS}
HOST_VETH=${HOST_VETH}
NAMESPACE_VETH=${NAMESPACE_VETH}
IPV4_HOST_CIDR=${IPV4_HOST_CIDR}
EOF

# 7. Sidecar that the systemd unit reads (EnvironmentFile) for the jailer flags.
sudo install -m 0644 /dev/stdin "${vm_directory}/jail.env" <<EOF
ATLAS_FC_UID=${ATLAS_FC_UID}
ATLAS_FC_GID=${ATLAS_FC_UID}
ATLAS_NETNS=${ATLAS_NETNS}
ATLAS_CGROUP_ARGS=${ATLAS_CGROUP_ARGS}
ATLAS_RESOURCE_ARGS=${ATLAS_RESOURCE_ARGS}
EOF

# 8. Enable and start the systemd unit.
sudo systemctl enable --now "firecracker-vm@${VIRTUAL_MACHINE_NAME}.service"

echo "Provisioned ${VIRTUAL_MACHINE_NAME}."
