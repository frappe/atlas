#!/bin/bash
# Phase 6 e2e: assert all on-host state for the VM is gone.
# - VM directory removed
# - systemd unit disabled (no enabled symlink)
# - TAP device gone
set -euo pipefail

: "${VIRTUAL_MACHINE_NAME:?}"
: "${TAP_DEVICE:?}"

vm_directory="/var/lib/atlas/virtual-machines/${VIRTUAL_MACHINE_NAME}"

if sudo test -d "$vm_directory"; then
    echo "expected ${vm_directory} to be gone" >&2
    exit 1
fi

unit="firecracker-vm@${VIRTUAL_MACHINE_NAME}.service"
state=$(sudo systemctl is-enabled "$unit" 2>&1 || true)
case "$state" in
    enabled|enabled-runtime|alias|static|linked|linked-runtime|generated|indirect|masked)
        echo "expected unit ${unit} to be disabled/removed; got: ${state}" >&2
        exit 1
        ;;
esac

if ip link show "$TAP_DEVICE" >/dev/null 2>&1; then
    echo "expected tap device ${TAP_DEVICE} to be gone" >&2
    exit 1
fi

echo "gone"
