#!/bin/bash
# Phase 6 e2e: assert the VM's systemd unit is NOT active.
set -euo pipefail

: "${VIRTUAL_MACHINE_NAME:?}"
if sudo systemctl is-active "firecracker-vm@${VIRTUAL_MACHINE_NAME}.service" >/dev/null 2>&1; then
    echo "expected unit to be inactive, but it is active" >&2
    exit 1
fi
echo "inactive"
