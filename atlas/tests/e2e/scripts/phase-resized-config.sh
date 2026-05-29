#!/bin/bash
# e2e: assert a VM's firecracker.json machine-config matches expected vcpu/mem
# and the rootfs has grown to at least DISK_GB.
set -euo pipefail

: "${VIRTUAL_MACHINE_NAME:?}"
: "${VCPUS:?}"
: "${MEMORY_MB:?}"
: "${DISK_GB:?}"

vm_directory="/var/lib/atlas/virtual-machines/${VIRTUAL_MACHINE_NAME}"
config_path="${vm_directory}/firecracker.json"
rootfs_path="${vm_directory}/rootfs.ext4"

actual_vcpus="$(sudo jq -r '."machine-config".vcpu_count' "$config_path")"
actual_mem="$(sudo jq -r '."machine-config".mem_size_mib' "$config_path")"
[ "$actual_vcpus" = "$VCPUS" ] || { echo "vcpu_count=${actual_vcpus} want=${VCPUS}" >&2; exit 1; }
[ "$actual_mem" = "$MEMORY_MB" ] || { echo "mem_size_mib=${actual_mem} want=${MEMORY_MB}" >&2; exit 1; }

want_bytes="$(( DISK_GB * 1024 * 1024 * 1024 ))"
actual_bytes="$(sudo stat -c %s "$rootfs_path")"
[ "$actual_bytes" -ge "$want_bytes" ] \
    || { echo "rootfs ${actual_bytes} bytes < want ${want_bytes}" >&2; exit 1; }

echo "resized vcpus=${actual_vcpus} mem=${actual_mem} disk>=${DISK_GB}G"
