#!/bin/bash
# Pause a Running VM: freeze its vCPUs via Firecracker's API socket. Guest RAM
# stays resident (this is not a shutdown). Idempotent: pausing an already-paused
# microVM keeps it paused (Firecracker returns 2xx either way).
#
# Inputs:
#   VIRTUAL_MACHINE_NAME  - UUID; selects the API socket

set -euo pipefail

: "${VIRTUAL_MACHINE_NAME:?required}"

socket="/var/lib/atlas/run/${VIRTUAL_MACHINE_NAME}.sock"
if [ ! -S "$socket" ]; then
    echo "API socket ${socket} not present; is the VM running?" >&2
    exit 1
fi

# --fail makes curl exit non-zero on a 4xx/5xx so a refused pause surfaces as a
# failed Task rather than a silent success.
sudo curl --fail --silent --show-error --unix-socket "$socket" \
    -X PATCH "http://localhost/vm" \
    -H "Content-Type: application/json" \
    -d '{"state": "Paused"}'

echo "Paused ${VIRTUAL_MACHINE_NAME}."
