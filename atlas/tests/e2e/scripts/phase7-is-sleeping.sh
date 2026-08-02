#!/bin/bash
# Phase 7 e2e: assert the VM is sleeping — unit inactive and SLEEPING marker present.

set -euo pipefail

: "${VIRTUAL_MACHINE_NAME:?}"

SLEEPING_MARKER="/var/lib/atlas/virtual-machines/${VIRTUAL_MACHINE_NAME}/sleeping"

for _ in $(seq 1 30); do
    if ! systemctl is-active --quiet "firecracker-vm@${VIRTUAL_MACHINE_NAME}.service" \
            && sudo test -f "${SLEEPING_MARKER}"; then
        exit 0
    fi
    sleep 1
done

if systemctl is-active --quiet "firecracker-vm@${VIRTUAL_MACHINE_NAME}.service"; then
    echo "VM unit is still active" >&2
elif ! sudo test -f "${SLEEPING_MARKER}"; then
    echo "SLEEPING marker missing at ${SLEEPING_MARKER}" >&2
fi
exit 1
