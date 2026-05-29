#!/bin/bash
# Resize a Stopped VM: set vCPU/memory in its firecracker config and grow the
# rootfs to DISK_GB. Firecracker reads machine-config only at boot, so the VM
# must be Stopped — the next Start picks up the new config. Disk only grows
# (the caller rejects shrink). Idempotent: re-running writes the same values
# and resize2fs is a no-op once the filesystem already fills the device.
#
# Inputs (environment variables):
#   VIRTUAL_MACHINE_NAME  - UUID; locates the VM directory
#   VCPUS                 - integer
#   MEMORY_MB             - integer
#   DISK_GB               - integer, target rootfs size

set -euo pipefail

: "${VIRTUAL_MACHINE_NAME:?required}"
: "${VCPUS:?required}"
: "${MEMORY_MB:?required}"
: "${DISK_GB:?required}"

vm_directory="/var/lib/atlas/virtual-machines/${VIRTUAL_MACHINE_NAME}"
config_path="${vm_directory}/firecracker.json"
rootfs_path="${vm_directory}/rootfs.ext4"

if [ ! -f "$config_path" ]; then
    echo "firecracker config ${config_path} missing; provision the VM first" >&2
    exit 1
fi

# 1. Rewrite machine-config in place. jq edits only the two keys, preserving
#    boot-source, drives and network-interfaces.
sudo jq \
    --argjson vcpus "$VCPUS" \
    --argjson mem "$MEMORY_MB" \
    '."machine-config".vcpu_count = $vcpus | ."machine-config".mem_size_mib = $mem' \
    "$config_path" | sudo install -m 0644 /dev/stdin "${config_path}.new"
sudo mv "${config_path}.new" "$config_path"

# 2. Grow the rootfs to DISK_GB. truncate only ever extends here (shrink is
#    rejected upstream); resize2fs then fills the larger device. No-op if the
#    filesystem already spans the device.
sudo truncate -s "${DISK_GB}G" "$rootfs_path"
sudo e2fsck -fy "$rootfs_path" >/dev/null 2>&1 || true
sudo resize2fs "$rootfs_path" >/dev/null

echo "Resized ${VIRTUAL_MACHINE_NAME}: ${VCPUS} vCPU, ${MEMORY_MB} MB, ${DISK_GB} GB."
